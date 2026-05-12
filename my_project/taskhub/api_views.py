import json
import hmac
import os
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from uuid import uuid4

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, IntegrityError, OperationalError, ProgrammingError, connections, transaction
from django.db.models import Case, CharField, Count, Exists, OuterRef, Q, Value, When
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from users.models import FrontendUser
from wallets.models import Wallet

from .models import ApiToken, MembershipLevelConfig, MobideaConversion, Task, TaskApplication, TaskCategory
from .mobidea import PROVIDER as MOBIDEA_PROVIDER, ensure_action_url, is_mobidea_task, parse_click_id
from .platform_publisher import get_task_platform_publisher, is_platform_publisher
from .task_lifecycle import (
    active_taker_count,
    after_publisher_accepts_application,
    expire_stale_pending_applications_for_applicant,
    effective_applicants_limit,
    is_mandatory_no_slot_cap,
    maybe_mark_task_completed_when_slots_full,
    touch_pending_application_activity,
)
from .task_rewards import grant_task_completion_reward
from .binding_usernames import account_binding_requires_bound_username, normalize_bound_username_for_task
from .twitter_client import (
    TwitterApiError,
    extract_tweet_id_from_url,
    extract_username_from_profile_url,
    normalize_twitter_username,
    user_follows_username,
    user_liked_tweet,
    user_retweeted_tweet,
)
from .integration_config import get_telegram_bot_token, get_twitter_bearer_token
from .instagram_client import normalize_instagram_username
from .instagram_apify_client import apify_instagram_configured, profile_contains_proof_via_apify
from .telegram_group_client import extract_telegram_chat_id_from_config, user_is_member_of_chat
from .locale_prefs import normalize_preferred_language
from .twitter_apify_client import (
    apify_twitter_error_is_service_side,
    apify_twitter_follow_configured,
    apify_twitter_repost_configured,
    user_follows_username_via_apify,
    user_retweeted_tweet_via_apify,
)
from .tiktok_apify_client import (
    apify_tiktok_configured,
    apify_tiktok_error_is_service_side,
    user_reposted_video_via_apify,
)
from .tiktok_client import extract_tiktok_video_id_from_url
from .telegram_push import _bot_dynamic_title, send_task_completion_message
from .youtube_client import channel_about_contains_proof, normalize_youtube_channel_identifier
from .referrals import resolve_inviter_from_start_token


def api_response(data=None, message="ok", code=0, status=200):
    return JsonResponse(
        {"code": code, "message": message, "data": data},
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


def api_error(message, code=4000, status=400):
    return api_response(data=None, message=message, code=code, status=status)


def parse_json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        raise ValueError("请求体不是合法 JSON")


def parse_interaction_config(value):
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    raise ValueError("interaction_config 须为 JSON 对象")


def parse_decimal(value, field_name):
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValueError(f"{field_name} 不是合法数字")
    if decimal_value < Decimal("0.00"):
        raise ValueError(f"{field_name} 不能小于 0")
    return decimal_value


def parse_positive_int(value, field_name, minimum=1):
    try:
        int_value = int(value)
    except (ValueError, TypeError):
        raise ValueError(f"{field_name} 必须是整数")
    if int_value < minimum:
        raise ValueError(f"{field_name} 不能小于 {minimum}")
    return int_value


def parse_deadline(value):
    if value in (None, ""):
        return None
    if isinstance(value, str):
        dt = parse_datetime(value)
        if dt:
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return dt
        date_value = parse_date(value)
        if date_value:
            return timezone.make_aware(
                datetime.combine(date_value, time(23, 59, 59)),
                timezone.get_current_timezone(),
            )
    raise ValueError("deadline 格式错误，请使用 ISO 时间字符串")


def get_bearer_token(request):
    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    if not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return token or None


def touch_frontend_user_last_seen(user_id, *, seen_at=None):
    if not user_id:
        return
    FrontendUser.objects.filter(pk=user_id).update(last_seen_at=seen_at or timezone.now())


def resolve_user_by_token(token_value):
    if not token_value:
        return None, None
    try:
        token = ApiToken.objects.select_related("user").get(key=token_value)
    except ApiToken.DoesNotExist:
        return None, None
    now = timezone.now()
    ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
    touch_frontend_user_last_seen(token.user_id, seen_at=now)
    token.last_used_at = now
    if getattr(token, "user", None) is not None:
        token.user.last_seen_at = now
    return token.user, token


def get_optional_api_user(request):
    token_value = get_bearer_token(request)
    user, token = resolve_user_by_token(token_value)
    request.api_user = user
    request.api_token = token
    return user


def _close_default_connection_if_safe() -> bool:
    """
    旧代码会在少数高并发写路径主动重置连接，降低同连接事务叠写带来的兼容问题。
    但在 Django TestCase 外层事务里关闭连接会直接打断测试事务，因此这里仅在安全时执行。
    """
    connection = connections[DEFAULT_DB_ALIAS]
    if connection.in_atomic_block:
        return False
    connection.close()
    return True


def require_api_login(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        token_value = get_bearer_token(request)
        user, token = resolve_user_by_token(token_value)
        if not user:
            return api_error("未登录或 token 无效", code=4010, status=401)
        request.api_user = user
        request.api_token = token
        return view_func(request, *args, **kwargs)

    return wrapper


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def me_ping_api(request):
    now = timezone.now()
    touch_frontend_user_last_seen(request.api_user.id, seen_at=now)
    return api_response({"server_time": now.isoformat()})


def serialize_user(user):
    return {
        "id": user.id,
        "phone": user.phone,
        "username": user.username,
        "membership_level": user.membership_level,
        "invite_code": user.invite_code,
        "status": user.status,
        "created_at": user.created_at.isoformat(),
        "telegram_id": user.telegram_id,
        "telegram_username": user.telegram_username,
        "preferred_language": user.preferred_language,
    }


def serialize_category(category):
    return {
        "id": category.id,
        "name": category.name,
        "slug": category.slug,
        "description": category.description,
        "sort_order": category.sort_order,
        "is_active": category.is_active,
    }


def binding_reference_url(task: Task) -> str | None:
    """
    与后台 interaction_config 对齐的「主参考链接」（入群邀请 / 账号绑定证明链接等）。
    前端「复制链接」应优先用本字段。
    """
    cfg = task.interaction_config or {}
    if task.interaction_type == Task.INTERACTION_JOIN_COMMUNITY:
        s = (cfg.get("invite_link") or cfg.get("telegram_invite_link") or "").strip()
        return s or None
    if task.interaction_type in {
        Task.INTERACTION_FOLLOW,
        Task.INTERACTION_REPOST,
        Task.INTERACTION_LIKE,
        Task.INTERACTION_COMMENT,
        Task.INTERACTION_WATCH_VIDEO,
        Task.INTERACTION_EXTERNAL_VOTE,
        Task.INTERACTION_SCREENSHOT_PROOF,
    }:
        for key in (
            "target_like_url",
            "target_post_url",
            "instagram_post_url",
            "target_profile_url",
            "tiktok_profile_url",
            "target_tweet_url",
            "target_video_url",
            "target_page_url",
            "target_url",
            "proof_target_url",
        ):
            s = (cfg.get(key) or "").strip()
            if s:
                return s
        return None
    if task.interaction_type != Task.INTERACTION_ACCOUNT_BINDING or not task.binding_platform:
        return None
    bp = task.binding_platform
    if bp == Task.BINDING_YOUTUBE:
        for key in ("youtube_proof_link", "youtube_url", "proof_link"):
            s = (cfg.get(key) or "").strip()
            if s:
                return s
        return None
    if bp == Task.BINDING_TWITTER:
        s = (cfg.get("target_tweet_url") or "").strip()
        return s or None
    if bp == Task.BINDING_TIKTOK:
        for key in ("target_video_url", "tiktok_video_url"):
            s = (cfg.get(key) or "").strip()
            if s:
                return s
        return None
    if bp == Task.BINDING_TELEGRAM:
        s = (cfg.get("telegram_invite_link") or cfg.get("invite_link") or "").strip()
        return s or None
    for key in (f"{bp}_proof_link", "profile_proof_link", "proof_link"):
        s = (cfg.get(key) or "").strip()
        if s:
            return s
    return None


def binding_verify_action(task: Task) -> str | None:
    """账号绑定时：前端应对 `my_application.id` 发起 POST 的路径后缀，如 verify-youtube。"""
    if task.interaction_type != Task.INTERACTION_ACCOUNT_BINDING or not task.binding_platform:
        return None
    return {
        Task.BINDING_TWITTER: "verify-twitter",
        Task.BINDING_YOUTUBE: "verify-youtube",
        Task.BINDING_INSTAGRAM: "verify-instagram",
        Task.BINDING_TIKTOK: "verify-tiktok",
    }.get(task.binding_platform)


def _join_community_telegram_verify_enabled(task: Task) -> bool:
    """配置了群 ID 且未显式关闭时，可走 Bot getChatMember 自动校验。"""
    if task.interaction_type != Task.INTERACTION_JOIN_COMMUNITY:
        return False
    cfg = task.interaction_config or {}
    chat = extract_telegram_chat_id_from_config(cfg)
    if not chat:
        return False
    if cfg.get("require_telegram_member") is False:
        return False
    return True


def _interaction_supports_explicit_self_verify(task: Task) -> bool:
    return (
        task.verification_mode == Task.VERIFY_USER_SELF
        and task.interaction_type in {
            Task.INTERACTION_FOLLOW,
            Task.INTERACTION_REPOST,
            Task.INTERACTION_LIKE,
            Task.INTERACTION_COMMENT,
        }
    )


def interaction_verify_action(task: Task) -> str | None:
    """非账号绑定类、但需要用户主动调接口完成校验时的路径后缀（如 verify-telegram-group）。"""
    if _join_community_telegram_verify_enabled(task):
        return "verify-telegram-group"
    if _interaction_supports_explicit_self_verify(task):
        return "verify-social-action"
    if task.verification_mode == Task.VERIFY_SCREENSHOT:
        return "upload-proof"
    return None


SOCIAL_ACTION_BOUND_PLATFORMS = {
    Task.BINDING_TWITTER,
    Task.BINDING_INSTAGRAM,
    Task.BINDING_TIKTOK,
}


def _accepted_platform_binding_application(user: FrontendUser, platform: str) -> TaskApplication | None:
    if not user or not platform:
        return None
    return (
        TaskApplication.objects.filter(
            applicant=user,
            status=TaskApplication.STATUS_ACCEPTED,
            task__interaction_type=Task.INTERACTION_ACCOUNT_BINDING,
            task__binding_platform=platform,
        )
        .select_related("task")
        .order_by("-decided_at", "-created_at")
        .first()
    )


def _social_action_binding_error(task: Task, user: FrontendUser) -> tuple[int, str] | None:
    if task.interaction_type not in {
        Task.INTERACTION_FOLLOW,
        Task.INTERACTION_REPOST,
        Task.INTERACTION_LIKE,
        Task.INTERACTION_COMMENT,
    }:
        return None
    if not task.binding_platform:
        return 4310, "该社交任务未配置目标平台，请联系管理员处理。"
    if task.binding_platform not in SOCIAL_ACTION_BOUND_PLATFORMS:
        return None
    if _accepted_platform_binding_application(user, task.binding_platform):
        return None
    platform_label = dict(Task.BINDING_PLATFORM_CHOICES).get(task.binding_platform, task.binding_platform)
    return 4311, f"请先绑定 {platform_label} 账号后再完成该任务。"


def _social_action_binding_api_error(task: Task, user: FrontendUser):
    binding_error = _social_action_binding_error(task, user)
    if not binding_error:
        return None
    code, msg = binding_error
    return api_error(msg, code=code, status=400)


def _vip_task_application_api_error(task: Task, user: FrontendUser, *, existing: TaskApplication | None = None):
    vip_error = _vip_task_application_error(task, user, existing=existing)
    if not vip_error:
        return None
    code, msg = vip_error
    return api_error(msg, code=code, status=400)


def _verify_twitter_social_action(task: Task, bound_username: str) -> tuple[bool, int, str, int]:
    cfg = task.interaction_config or {}

    if task.interaction_type == Task.INTERACTION_FOLLOW:
        target = normalize_twitter_username((cfg.get("target_follow_username") or "").strip())
        if not target:
            target = extract_username_from_profile_url((cfg.get("target_profile_url") or "").strip())
        if not target:
            return False, 4313, "Twitter 关注任务缺少目标账号配置，请联系管理员处理。", 400
        if apify_twitter_follow_configured():
            ok, err = user_follows_username_via_apify(bound_username, target)
            if err and ok is False and err != "并未检测到关注，请先完成关注后再试。":
                status = 503 if apify_twitter_error_is_service_side(err) else 400
                return False, 4314, err, status
            if not ok:
                return False, 4315, err or "并未检测到关注，请先完成关注后再试。", 400
            return True, 0, "", 200
        bearer = get_twitter_bearer_token()
        if not bearer:
            return False, 4312, "Twitter 关注校验服务暂不可用，请稍后再试。", 503
        try:
            ok = user_follows_username(bearer, bound_username, target)
        except ValueError as exc:
            return _twitter_verify_error(exc, action_label="关注", error_code=4314)
        if not ok:
            return False, 4315, "并未检测到关注，请先完成关注后再试。", 400
        return True, 0, "", 200

    if task.interaction_type == Task.INTERACTION_REPOST:
        tweet_url = (
            (cfg.get("target_tweet_url") or "")
            or (cfg.get("target_post_url") or "")
            or (cfg.get("target_repost_url") or "")
        ).strip()
        tweet_id = extract_tweet_id_from_url(tweet_url)
        if not tweet_id:
            return False, 4319, "Twitter 转发任务缺少目标推文配置，请联系管理员处理。", 400
        if apify_twitter_repost_configured():
            ok, err = user_retweeted_tweet_via_apify(tweet_url, bound_username)
            if err and ok is False and err != "并未检测到转发，请先完成转发后再试。":
                status = 503 if apify_twitter_error_is_service_side(err) else 400
                return False, 4320, err, status
            if not ok:
                return False, 4321, err or "并未检测到转发，请先完成转发后再试。", 400
            return True, 0, "", 200
        bearer = get_twitter_bearer_token()
        if not bearer:
            return False, 4312, "Twitter 转发校验服务暂不可用，请稍后再试。", 503
        try:
            ok = user_retweeted_tweet(bearer, tweet_id, bound_username)
        except ValueError as exc:
            return _twitter_verify_error(exc, action_label="转发", error_code=4320)
        if not ok:
            return False, 4321, "并未检测到转发，请先完成转发后再试。", 400
        return True, 0, "", 200

    if task.interaction_type == Task.INTERACTION_LIKE:
        bearer = get_twitter_bearer_token()
        if not bearer:
            return False, 4312, "Twitter 点赞校验服务暂不可用，请稍后再试。", 503
        tweet_url = (
            (cfg.get("target_like_url") or "")
            or (cfg.get("target_tweet_url") or "")
            or (cfg.get("target_post_url") or "")
        ).strip()
        tweet_id = extract_tweet_id_from_url(tweet_url)
        if not tweet_id:
            return False, 4316, "Twitter 点赞任务缺少目标推文配置，请联系管理员处理。", 400
        try:
            ok = user_liked_tweet(bearer, tweet_id, bound_username)
        except ValueError as exc:
            return _twitter_verify_error(exc, action_label="点赞", error_code=4317)
        if not ok:
            return False, 4318, "并未检测到点赞，请先完成点赞后再试。", 400
        return True, 0, "", 200

    return True, 0, "", 200


def _twitter_verify_error(exc: ValueError, *, action_label: str, error_code: int) -> tuple[bool, int, str, int]:
    if isinstance(exc, TwitterApiError):
        body_lower = exc.body.lower()
        if exc.status_code == 402 or "creditsdepleted" in body_lower or "credit" in body_lower:
            return (
                False,
                error_code,
                f"Twitter {action_label}校验额度已耗尽，请联系管理员补充 X API Credits 后重试。",
                503,
            )
        if exc.status_code in {401, 403}:
            return False, error_code, f"Twitter {action_label}校验权限不足，请联系管理员检查 API 权限配置。", 503
        if exc.status_code == 429:
            return False, error_code, f"Twitter {action_label}校验过于频繁，请稍后再试。", 429
    return False, error_code, f"暂时无法完成 Twitter {action_label}校验，请稍后再试。", 502


def _verify_tiktok_social_action(task: Task, bound_username: str) -> tuple[bool, int, str, int]:
    if task.interaction_type != Task.INTERACTION_REPOST:
        return True, 0, "", 200
    cfg = task.interaction_config or {}
    video_url = (
        (cfg.get("target_video_url") or "")
        or (cfg.get("tiktok_video_url") or "")
        or (cfg.get("target_repost_url") or "")
    ).strip()
    if not extract_tiktok_video_id_from_url(video_url):
        return False, 4322, "TikTok 转发任务缺少目标视频配置，请联系管理员处理。", 400
    if not apify_tiktok_configured():
        return False, 4323, "TikTok 转发校验服务暂不可用，请稍后再试。", 503
    ok, err = user_reposted_video_via_apify(bound_username, video_url)
    if ok:
        return True, 0, "", 200
    status = 503 if apify_tiktok_error_is_service_side(err) else 400
    return False, 4324, err or "并未检测到转发，请确认已完成转发后再试。", status


def serialize_task(task, current_user=None, include_contact=False):
    real_application_count = getattr(task, "application_count", None)
    if real_application_count is None:
        real_application_count = task.applications.count()
    application_count = task.display_application_count(real_application_count)
    virtual_application_count = task.display_virtual_application_count()
    can_view_contact = include_contact or (current_user and current_user.id == task.publisher_id)
    language = getattr(current_user, "preferred_language", None) if current_user is not None else None
    data = {
        "id": task.id,
        "title": _bot_dynamic_title(task.title, language),
        "description": task.description,
        "status": task.status,
        "status_display": task.get_status_display(),
        "budget": str(task.budget),
        "reward_unit": task.reward_unit,
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "region": task.region,
        "applicants_limit": task.applicants_limit,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "category": (
            {"id": task.category_id, "name": task.category.name, "slug": task.category.slug}
            if task.category
            else None
        ),
        "publisher": {
            "id": task.publisher_id,
            "username": task.publisher.username,
            "membership_level": task.publisher.membership_level,
        },
        "application_count": application_count,
        "real_application_count": real_application_count,
        "virtual_application_count": virtual_application_count,
        "virtual_application_base_count": int(task.virtual_application_count or 0),
        "virtual_application_auto_increment_count": int(task.virtual_auto_increment_count or 0),
        "interaction_type": task.interaction_type,
        "interaction_type_display": task.get_interaction_type_display(),
        "binding_platform": task.binding_platform or None,
        "binding_platform_display": task.get_binding_platform_display() if task.binding_platform else None,
        "verification_mode": task.verification_mode,
        "verification_mode_display": (
            task.get_verification_mode_display() if task.verification_mode else None
        ),
        "interaction_config": task.interaction_config or {},
        "binding_reference_url": binding_reference_url(task),
        "binding_verify_action": binding_verify_action(task),
        "interaction_verify_action": interaction_verify_action(task),
        "is_mandatory": task.is_mandatory,
        "is_vip_exclusive": task.is_vip_exclusive,
        "task_zone": "vip" if task.is_vip_exclusive else "public",
        "task_list_order": task.task_list_order,
        "reward_usdt": str(task.reward_usdt) if task.reward_usdt is not None else None,
        "reward_th_coin": str(task.reward_th_coin) if task.reward_th_coin is not None else None,
    }
    if can_view_contact:
        data["contact_name"] = task.contact_name
        data["contact_phone"] = task.contact_phone
    return data


def _task_platform_key(task: Task) -> str:
    """
    任务中心 Tab / 图标用：优先 binding_platform；入群类为 telegram；
    否则尝试用分类 slug（twitter/x/tiktok/youtube 等）。
    """
    if task.binding_platform:
        return task.binding_platform
    if task.interaction_type == Task.INTERACTION_JOIN_COMMUNITY:
        return "telegram"
    if task.category_id and task.category:
        slug = (task.category.slug or "").lower()
        if slug in ("x", "twitter"):
            return "twitter"
        if slug in ("tiktok", "youtube", "instagram", "facebook"):
            return slug
    return "other"


def _membership_level_config_for_user(user: FrontendUser | None) -> MembershipLevelConfig | None:
    if user is None:
        return None
    try:
        return MembershipLevelConfig.for_level(getattr(user, "membership_level", 0))
    except (OperationalError, ProgrammingError):
        return None


def _vip_task_day_bounds() -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    local_now = timezone.localtime(timezone.now(), tz)
    start = timezone.make_aware(datetime.combine(local_now.date(), time.min), tz)
    return start, start + timedelta(days=1)


def _vip_task_usage_count(user: FrontendUser) -> int:
    day_start, day_end = _vip_task_day_bounds()
    return (
        TaskApplication.objects.filter(
            applicant=user,
            task__is_vip_exclusive=True,
            status=TaskApplication.STATUS_ACCEPTED,
        ).filter(
            Q(reward_paid_at__gte=day_start, reward_paid_at__lt=day_end)
            | Q(decided_at__gte=day_start, decided_at__lt=day_end)
            | Q(self_verified_at__gte=day_start, self_verified_at__lt=day_end)
        )
        .count()
    )


def _vip_task_access_snapshot(user: FrontendUser | None) -> dict:
    if user is None:
        return {
            "has_access": False,
            "can_apply_now": False,
            "membership_level": None,
            "membership_name": None,
            "used_today": 0,
            "daily_limit": None,
            "daily_limit_label": "需 VIP1+",
            "remaining_today": 0,
            "remaining_today_label": "登录后查看",
            "unlimited": False,
            "hint": "登录后可查看 VIP 任务专区并按会员等级领取任务。",
        }

    try:
        membership_level = int(getattr(user, "membership_level", 0) or 0)
    except (TypeError, ValueError):
        membership_level = 0

    level_config = _membership_level_config_for_user(user)
    if level_config is None or not level_config.can_claim_official_tasks:
        return {
            "has_access": False,
            "can_apply_now": False,
            "membership_level": membership_level,
            "membership_name": getattr(level_config, "name", None),
            "used_today": 0,
            "daily_limit": None,
            "daily_limit_label": "需 VIP1+",
            "remaining_today": 0,
            "remaining_today_label": "0 次",
            "unlimited": False,
            "hint": "VIP 任务专区仅限 VIP1 及以上会员领取，请先升级会员。",
        }

    used_today = _vip_task_usage_count(user)
    unlimited = bool(level_config.unlimited_tasks or level_config.daily_official_task_limit is None)
    daily_limit = None if unlimited else int(level_config.daily_official_task_limit or 0)
    remaining_today = None if unlimited else max(daily_limit - used_today, 0)
    if unlimited:
        hint = f"{level_config.name} 当前可不限量领取 VIP 任务。"
    elif remaining_today <= 0:
        hint = f"今日 VIP 任务领取次数已用完（{used_today}/{daily_limit}）。"
    else:
        hint = f"今日已接 {used_today}/{daily_limit} 个 VIP 任务，还可接 {remaining_today} 个。"
    return {
        "has_access": True,
        "can_apply_now": unlimited or bool(remaining_today and remaining_today > 0),
        "membership_level": membership_level,
        "membership_name": level_config.name,
        "used_today": used_today,
        "daily_limit": daily_limit,
        "daily_limit_label": "不限" if unlimited else f"{daily_limit} 次/天",
        "remaining_today": remaining_today,
        "remaining_today_label": "不限" if unlimited else f"{remaining_today} 次",
        "unlimited": unlimited,
        "hint": hint,
    }


def _vip_task_application_error(
    task: Task,
    user: FrontendUser,
    *,
    existing: TaskApplication | None = None,
) -> tuple[int, str] | None:
    if not task.is_vip_exclusive:
        return None
    if existing is not None and existing.status in {
        TaskApplication.STATUS_PENDING,
        TaskApplication.STATUS_ACCEPTED,
    }:
        return None
    snapshot = _vip_task_access_snapshot(user)
    if not snapshot["has_access"]:
        return 4401, snapshot["hint"]
    if not snapshot["can_apply_now"]:
        return 4402, snapshot["hint"]
    return None


def task_apply_precheck_for_list(task: Task, user: FrontendUser) -> tuple[bool, int, str | None]:
    """列表/卡片用：当前用户点「开始」是否可能成功；POST 仍以 task_apply 为准。"""
    if task.publisher_id == user.id:
        return False, 4033, "不能报名自己发布的任务"
    if task.status != Task.STATUS_OPEN:
        return False, 4034, "当前任务不可报名"
    existing = TaskApplication.objects.filter(task=task, applicant=user).first()
    if existing:
        if existing.status == TaskApplication.STATUS_ACCEPTED:
            # 入群类一旦录用即视为完成；其余沿用「已结奖/无应付奖励」判定
            if task.interaction_type == Task.INTERACTION_JOIN_COMMUNITY or _task_application_truly_done(existing, task):
                return False, 4101, "您已完成该任务"
        if existing.status == TaskApplication.STATUS_REJECTED:
            return False, 4036, "该任务报名已被拒绝，无法再次提交"
        if existing.status == TaskApplication.STATUS_CANCELLED:
            vip_error = _vip_task_application_error(task, user, existing=existing)
            if vip_error:
                code, msg = vip_error
                return False, code, msg
            binding_error = _social_action_binding_error(task, user)
            if binding_error:
                code, msg = binding_error
                return False, code, msg
            if not is_mandatory_no_slot_cap(task):
                if active_taker_count(task) >= effective_applicants_limit(task):
                    return False, 4035, "该任务接取人数已满"
            return True, 0, None
        binding_error = _social_action_binding_error(task, user)
        if binding_error:
            code, msg = binding_error
            return False, code, msg
        # 本人已有报名：应允许继续（同步信息 / 校验），不因名额已满而误标为不可点
        return True, 0, None
    vip_error = _vip_task_application_error(task, user)
    if vip_error:
        code, msg = vip_error
        return False, code, msg
    binding_error = _social_action_binding_error(task, user)
    if binding_error:
        code, msg = binding_error
        return False, code, msg
    if not is_mandatory_no_slot_cap(task):
        if active_taker_count(task) >= effective_applicants_limit(task):
            return False, 4035, "该任务接取人数已满"
    return True, 0, None


def enrich_task_apply_hints(task: Task, data: dict, current_user: FrontendUser | None) -> None:
    """便于前端「开始」：未登录、本人发布、已满员等可直接提示，避免点了无反馈。"""
    if not current_user:
        data["can_apply"] = False
        data["apply_precheck_code"] = 4010
        data["apply_precheck_message"] = "未登录或 token 无效，请先登录后再点「开始」"
        return
    ok, code, msg = task_apply_precheck_for_list(task, current_user)
    data["can_apply"] = ok
    data["apply_precheck_code"] = code
    data["apply_precheck_message"] = msg or ""


def enrich_task_card_fields(task: Task, data: dict) -> None:
    """任务中心卡片：platform_key、名额进度、已录用人数（就地写入 data）。"""
    ac = getattr(task, "accepted_count", None)
    if ac is None:
        ac = task.applications.filter(status=TaskApplication.STATUS_ACCEPTED).count()
    data["accepted_count"] = ac
    al = effective_applicants_limit(task)
    display_applicants = data.get("application_count")
    if not isinstance(display_applicants, int):
        real_application_count = getattr(task, "application_count", None)
        if real_application_count is None:
            real_application_count = task.applications.count()
        display_applicants = task.display_application_count(real_application_count)
    data["slot_progress_percent"] = min(100, int(display_applicants * 100 / al))
    data["platform_key"] = _task_platform_key(task)


def _my_application_brief(app: TaskApplication | None) -> dict | None:
    if not app:
        return None
    return {
        "id": app.id,
        "status": app.status,
        "status_display": app.get_status_display(),
        "bound_username": app.bound_username,
        "self_verified_at": app.self_verified_at.isoformat() if app.self_verified_at else None,
        "created_at": app.created_at.isoformat(),
    }


def _mandatory_task_card_done_for_user(app: TaskApplication | None, task: Task) -> bool:
    """
    首页必做卡片「是否可隐藏」：
    - 必做且不按名额关单（账号绑定 / 入群）类：用户一旦 accepted 即视为该用户侧完成，直接隐藏卡片。
    - 其它玩法沿用既有「已结奖或无应付奖励」判定。
    """
    if not app or app.status != TaskApplication.STATUS_ACCEPTED:
        return False
    if is_mandatory_no_slot_cap(task):
        return True
    return _task_application_truly_done(app, task)


def build_mandatory_task_items(current_user):
    """首页必做列表与任务中心共用。"""
    if current_user:
        expire_stale_pending_applications_for_applicant(current_user.id)
    qs = list(
        Task.objects.filter(is_mandatory=True, status=Task.STATUS_OPEN)
        .select_related("publisher", "category")
        .annotate(
            application_count=Count("applications"),
            accepted_count=Count(
                "applications", filter=Q(applications__status=TaskApplication.STATUS_ACCEPTED)
            ),
        )
        .order_by("-task_list_order", "-id")
    )
    app_by_task: dict[int, TaskApplication] = {}
    if current_user and qs:
        tids = [t.id for t in qs]
        for a in TaskApplication.objects.filter(applicant=current_user, task_id__in=tids):
            app_by_task[a.task_id] = a

    items = []
    for t in qs:
        app = app_by_task.get(t.id) if current_user else None
        # 必做入群/绑定类按「已录用即隐藏」；其它玩法沿用「已结奖/无应付奖励」判定
        if _mandatory_task_card_done_for_user(app, t):
            continue
        data = serialize_task(t, current_user=current_user)
        enrich_task_card_fields(t, data)
        if app:
            data["my_application"] = _my_application_brief(app)
        else:
            data["my_application"] = None if current_user else None
        enrich_task_apply_hints(t, data, current_user)
        items.append(data)

    # 必做任务已非 open（关闭/完成等）时，原先整段从列表消失；若用户曾「已录用」且仍未结奖/未完成，补一条卡片便于入口与任务记录对齐
    shown_ids = {t.id for t in qs}
    if current_user:
        stuck_qs = Task.objects.filter(is_mandatory=True).exclude(status=Task.STATUS_OPEN).filter(
            applications__applicant=current_user,
            applications__status=TaskApplication.STATUS_ACCEPTED,
        )
        if shown_ids:
            stuck_qs = stuck_qs.exclude(pk__in=shown_ids)
        stuck_qs = (
            stuck_qs.select_related("publisher", "category")
            .annotate(
                application_count=Count("applications"),
                accepted_count=Count(
                    "applications", filter=Q(applications__status=TaskApplication.STATUS_ACCEPTED)
                ),
            )
            .distinct()
            .order_by("-task_list_order", "-id")
        )
        for t in stuck_qs:
            app = TaskApplication.objects.filter(
                task=t, applicant=current_user, status=TaskApplication.STATUS_ACCEPTED
            ).first()
            if _mandatory_task_card_done_for_user(app, t):
                continue
            if not app:
                continue
            data = serialize_task(t, current_user=current_user)
            enrich_task_card_fields(t, data)
            data["my_application"] = _my_application_brief(app)
            data["mandatory_task_stale"] = True
            data["mandatory_task_stale_hint"] = (
                "任务已结束或暂不可接；您曾接取但未完成。若运营将任务改回「可报名」，请再点报名或从任务记录查看。"
            )
            enrich_task_apply_hints(t, data, current_user)
            items.append(data)
    return items


@csrf_exempt
@require_http_methods(["GET"])
def tasks_center_api(request):
    """
    任务中心页：分类 Tab + 必做任务 + 可用任务（分页），并附带卡片展示辅助字段。
    """
    current_user = get_optional_api_user(request)
    if current_user:
        expire_stale_pending_applications_for_applicant(current_user.id)

    cats = list(TaskCategory.objects.filter(is_active=True).order_by("-sort_order", "id"))
    category_items = [{"id": None, "name": "全部", "slug": "all", "is_all": True}] + [
        serialize_category(c) for c in cats
    ]

    mandatory_items = build_mandatory_task_items(current_user)
    now_iso = timezone.now().isoformat()

    base_qs = (
        Task.objects.filter(status=Task.STATUS_OPEN, is_mandatory=False)
        .select_related("publisher", "category")
        .annotate(
            application_count=Count("applications"),
            accepted_count=Count(
                "applications", filter=Q(applications__status=TaskApplication.STATUS_ACCEPTED)
            ),
        )
    )

    category_id = (request.GET.get("category_id") or "").strip()
    if category_id and category_id != "all":
        try:
            base_qs = base_qs.filter(category_id=int(category_id))
        except ValueError:
            return api_error("category_id 须为整数，或使用分类 slug 通过其它接口筛选", code=4053, status=400)

    keyword = (request.GET.get("keyword") or "").strip()
    if keyword:
        base_qs = base_qs.filter(Q(title__icontains=keyword) | Q(description__icontains=keyword))

    binding_platform = (request.GET.get("binding_platform") or "").strip()
    valid_bp = {c[0] for c in Task.BINDING_PLATFORM_CHOICES}
    if binding_platform:
        if binding_platform not in valid_bp:
            return api_error("binding_platform 不合法", code=4044, status=400)
        base_qs = base_qs.filter(binding_platform=binding_platform)

    if current_user:
        has_done_for_current_user = TaskApplication.objects.filter(
            task_id=OuterRef("pk"),
            applicant_id=current_user.id,
            status=TaskApplication.STATUS_ACCEPTED,
        ).filter(
            Q(task__interaction_type=Task.INTERACTION_JOIN_COMMUNITY)
            | Q(reward_paid_at__isnull=False)
            | ~(
                Q(task__reward_usdt__isnull=False, task__reward_usdt__gt=0)
                | Q(task__reward_th_coin__isnull=False, task__reward_th_coin__gt=0)
            )
        )
        # 可用任务排除当前用户已完成项（尤其是入群类，避免完成后仍显示在可用列表）
        base_qs = base_qs.annotate(done_for_current_user=Exists(has_done_for_current_user)).filter(
            done_for_current_user=False
        )

    vip_qs = base_qs.filter(is_vip_exclusive=True)
    available_qs = base_qs.filter(is_vip_exclusive=False)

    try:
        page = parse_positive_int(request.GET.get("page", 1), "page", minimum=1)
        page_size = parse_positive_int(request.GET.get("page_size", 20), "page_size", minimum=1)
    except ValueError as exc:
        return api_error(str(exc), code=4016, status=400)
    page_size = min(page_size, 50)

    total = available_qs.count()
    offset = (page - 1) * page_size
    page_tasks = list(available_qs.order_by("-updated_at", "-id")[offset : offset + page_size])
    vip_total = vip_qs.count()
    vip_tasks = list(vip_qs.order_by("-updated_at", "-id")[:20])

    app_by_task: dict[int, TaskApplication] = {}
    lookup_tasks = [*page_tasks, *vip_tasks]
    if current_user and lookup_tasks:
        tids = [t.id for t in lookup_tasks]
        for a in TaskApplication.objects.filter(applicant=current_user, task_id__in=tids):
            app_by_task[a.task_id] = a

    available_items = []
    for t in page_tasks:
        data = serialize_task(t, current_user=current_user)
        enrich_task_card_fields(t, data)
        data["my_application"] = (
            _my_application_brief(app_by_task.get(t.id)) if current_user else None
        )
        enrich_task_apply_hints(t, data, current_user)
        available_items.append(data)

    vip_items = []
    for t in vip_tasks:
        data = serialize_task(t, current_user=current_user)
        enrich_task_card_fields(t, data)
        data["my_application"] = (
            _my_application_brief(app_by_task.get(t.id)) if current_user else None
        )
        enrich_task_apply_hints(t, data, current_user)
        vip_items.append(data)

    return api_response(
        {
            "categories": category_items,
            "mandatory": {"items": mandatory_items, "updated_at": now_iso},
            "vip_zone": {
                "items": vip_items,
                "updated_at": now_iso,
                "summary": _vip_task_access_snapshot(current_user),
                "pagination": {"page": 1, "page_size": 20, "total": vip_total},
            },
            "available": {
                "items": available_items,
                "pagination": {"page": page, "page_size": page_size, "total": total},
                "updated_at": now_iso,
            },
        }
    )


def serialize_application(application, request=None):
    proof_url = None
    if application.proof_image:
        proof_url = application.proof_image.url
        if request:
            proof_url = request.build_absolute_uri(proof_url)
    return {
        "id": application.id,
        "task_id": application.task_id,
        "task_title": application.task.title if hasattr(application, "task") else None,
        "verification_reference_url": (
            binding_reference_url(application.task) if hasattr(application, "task") else None
        ),
        "binding_verify_action": (
            binding_verify_action(application.task) if hasattr(application, "task") else None
        ),
        "interaction_verify_action": (
            interaction_verify_action(application.task) if hasattr(application, "task") else None
        ),
        "applicant": {
            "id": application.applicant_id,
            "username": application.applicant.username,
            "phone": application.applicant.phone,
        },
        "proposal": application.proposal,
        "bound_username": application.bound_username,
        "proof_image_url": proof_url,
        "self_verified_at": application.self_verified_at.isoformat() if application.self_verified_at else None,
        "reward_paid_at": application.reward_paid_at.isoformat() if application.reward_paid_at else None,
        "quoted_price": str(application.quoted_price) if application.quoted_price is not None else None,
        "status": application.status,
        "status_display": application.get_status_display(),
        "created_at": application.created_at.isoformat(),
        "decided_at": application.decided_at.isoformat() if application.decided_at else None,
    }


def _mobidea_apply_payload(application: TaskApplication, request=None) -> dict:
    data = serialize_application(application, request)
    action_url = ensure_action_url(application)
    data["external_provider"] = MOBIDEA_PROVIDER
    data["external_click_id"] = application.external_click_id
    data["external_action_url"] = action_url
    data["verification_reference_url"] = action_url
    data["verification_share_url"] = action_url
    return {
        "application": data,
        "external_provider": MOBIDEA_PROVIDER,
        "external_action_url": action_url,
        "verification_reference_url": action_url,
    }


def _serialize_apply_payload(application: TaskApplication, request=None) -> dict:
    if hasattr(application, "task") and is_mobidea_task(application.task):
        return _mobidea_apply_payload(application, request)
    return {"application": serialize_application(application, request)}


# —— 任务记录页（Mini App「任务记录」Tab）——
# `record_status` 分类与列表 annotate 的 Case/When 顺序须一致，勿单独改一半。
RECORD_TAB_ALL = "all"
RECORD_STATUS_IN_PROGRESS = "in_progress"
RECORD_STATUS_UNDER_REVIEW = "under_review"
RECORD_STATUS_COMPLETED = "completed"
RECORD_STATUS_INVALID = "invalid"
RECORD_STATUS_LABELS = {
    RECORD_STATUS_IN_PROGRESS: "进行中",
    RECORD_STATUS_UNDER_REVIEW: "审核中",
    RECORD_STATUS_COMPLETED: "已完成",
    RECORD_STATUS_INVALID: "已失效",
}


def _task_application_payable_reward_q() -> Q:
    return Q(task__reward_usdt__isnull=False, task__reward_usdt__gt=0) | Q(
        task__reward_th_coin__isnull=False, task__reward_th_coin__gt=0
    )


TASK_RECORD_USER_CONTINUE_INTERACTIONS = (
    Task.INTERACTION_JOIN_COMMUNITY,
    Task.INTERACTION_ACCOUNT_BINDING,
    Task.INTERACTION_FOLLOW,
    Task.INTERACTION_REPOST,
    Task.INTERACTION_LIKE,
    Task.INTERACTION_COMMENT,
)


def _task_applications_queryset_for_record_tabs(user):
    open_tasks = Q(task__status__in=(Task.STATUS_OPEN, Task.STATUS_IN_PROGRESS))
    pay = _task_application_payable_reward_q()
    proof_ready = Q(proof_image__isnull=False) & ~Q(proof_image="")

    return (
        TaskApplication.objects.filter(applicant=user)
        .select_related("task", "task__category", "task__publisher")
        .annotate(
            record_status=Case(
                When(
                    Q(status__in=(TaskApplication.STATUS_REJECTED, TaskApplication.STATUS_CANCELLED)),
                    then=Value(RECORD_STATUS_INVALID),
                ),
                When(Q(status=TaskApplication.STATUS_PENDING) & ~open_tasks, then=Value(RECORD_STATUS_INVALID)),
                When(
                    Q(status=TaskApplication.STATUS_ACCEPTED)
                    & Q(reward_paid_at__isnull=True)
                    & pay
                    & Q(task__status__in=(Task.STATUS_CLOSED, Task.STATUS_DRAFT)),
                    then=Value(RECORD_STATUS_INVALID),
                ),
                When(
                    Q(status=TaskApplication.STATUS_ACCEPTED)
                    & (Q(reward_paid_at__isnull=False) | ~pay),
                    then=Value(RECORD_STATUS_COMPLETED),
                ),
                When(
                    Q(status=TaskApplication.STATUS_PENDING)
                    & open_tasks
                    & Q(task__verification_mode=Task.VERIFY_SCREENSHOT)
                    & ~proof_ready,
                    then=Value(RECORD_STATUS_IN_PROGRESS),
                ),
                When(
                    Q(status=TaskApplication.STATUS_PENDING)
                    & open_tasks
                    & Q(task__interaction_type__in=TASK_RECORD_USER_CONTINUE_INTERACTIONS),
                    then=Value(RECORD_STATUS_IN_PROGRESS),
                ),
                When(
                    Q(status=TaskApplication.STATUS_PENDING) & open_tasks,
                    then=Value(RECORD_STATUS_UNDER_REVIEW),
                ),
                When(
                    Q(status=TaskApplication.STATUS_ACCEPTED)
                    & Q(task__verification_mode=Task.VERIFY_SCREENSHOT)
                    & proof_ready
                    & Q(reward_paid_at__isnull=True)
                    & pay,
                    then=Value(RECORD_STATUS_UNDER_REVIEW),
                ),
                default=Value(RECORD_STATUS_IN_PROGRESS),
                output_field=CharField(max_length=20),
            )
        )
    )


def _task_record_time_fields(app: TaskApplication, record_status: str) -> tuple[object | None, str]:
    if record_status == RECORD_STATUS_INVALID:
        label = "更新时间"
        dt = app.decided_at or app.updated_at
    elif record_status == RECORD_STATUS_COMPLETED:
        label = "完成时间"
        dt = app.reward_paid_at or app.decided_at or app.updated_at
    elif record_status == RECORD_STATUS_UNDER_REVIEW:
        label = "提交时间"
        if app.status == TaskApplication.STATUS_PENDING:
            dt = app.created_at
        else:
            dt = app.self_verified_at or app.updated_at
    else:
        label = "更新时间"
        dt = app.updated_at
    return dt, label


def _format_task_record_time(dt) -> tuple[str | None, str | None]:
    if not dt:
        return None, None
    lt = timezone.localtime(dt)
    return lt.isoformat(), lt.strftime("%Y-%m-%d %H:%M")


def _task_record_reward_strings(task: Task) -> dict:
    rewards: dict = {"usdt": None, "th_coin": None}
    if task.reward_usdt is not None and task.reward_usdt > 0:
        s = format(task.reward_usdt, "f").rstrip("0").rstrip(".") or "0"
        rewards["usdt"] = f"+{s} USDT"
    if task.reward_th_coin is not None and task.reward_th_coin > 0:
        s = format(task.reward_th_coin, "f").rstrip("0").rstrip(".") or "0"
        rewards["th_coin"] = f"+{s} TH"
    return rewards


def _task_record_can_continue(app: TaskApplication) -> bool:
    if app.record_status != RECORD_STATUS_IN_PROGRESS:
        return False
    if app.status != TaskApplication.STATUS_PENDING:
        return False
    if app.task.interaction_type in TASK_RECORD_USER_CONTINUE_INTERACTIONS:
        return True
    return bool(binding_verify_action(app.task) or interaction_verify_action(app.task))


def serialize_task_record_item(app: TaskApplication, user: FrontendUser | None = None):
    task = app.task
    language = getattr(user, "preferred_language", None) if user is not None else None
    record_status = app.record_status
    dt, time_label = _task_record_time_fields(app, record_status)
    iso, disp = _format_task_record_time(dt)
    return {
        "id": app.id,
        "task_id": task.id,
        "title": _bot_dynamic_title(task.title, language),
        "platform_key": _task_platform_key(task),
        "icon_url": None,
        "record_status": record_status,
        "record_status_display": RECORD_STATUS_LABELS.get(record_status, record_status),
        "rewards": _task_record_reward_strings(task),
        "time": {"label": time_label, "at": iso, "display": disp},
        "can_continue": _task_record_can_continue(app),
    }


@csrf_exempt
@require_http_methods(["GET"])
def health_api(request):
    return api_response(
        {
            "service": "taskhub-api",
            "time": timezone.now().isoformat(),
            "instagram_apify_configured": apify_instagram_configured(),
            "twitter_apify_follow_configured": apify_twitter_follow_configured(),
            "twitter_apify_repost_configured": apify_twitter_repost_configured(),
            "tiktok_apify_configured": apify_tiktok_configured(),
            "telegram_bot_configured": bool(get_telegram_bot_token()),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def register_api(request):
    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    phone = (body.get("phone") or "").strip()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    pay_password = body.get("pay_password") or ""
    membership_level = body.get("membership_level", 1)

    if len(phone) != 11 or not phone.isdigit():
        return api_error("手机号必须为 11 位数字", code=4002, status=400)
    if not username:
        return api_error("username 不能为空", code=4003, status=400)
    if len(password) < 6:
        return api_error("password 不能少于 6 位", code=4004, status=400)

    try:
        membership_level = int(membership_level)
    except (ValueError, TypeError):
        return api_error("membership_level 必须是整数", code=4005, status=400)

    if FrontendUser.objects.filter(phone=phone).exists():
        return api_error("手机号已注册", code=4006, status=409)
    if FrontendUser.objects.filter(username=username).exists():
        return api_error("用户名已被占用", code=4007, status=409)

    ref_raw = (body.get("invite_code") or body.get("ref") or "").strip()
    referrer_obj = resolve_inviter_from_start_token(ref_raw) if ref_raw else None

    with transaction.atomic():
        user = FrontendUser.objects.create(
            phone=phone,
            username=username,
            password=password,
            pay_password=pay_password,
            membership_level=membership_level,
            referrer=referrer_obj,
        )
        Wallet.objects.get_or_create(user=user)
        token = ApiToken.issue_for_user(user)

    return api_response(
        {
            "token": token.key,
            "user": serialize_user(user),
        },
        message="注册成功",
    )


@csrf_exempt
@require_http_methods(["POST"])
def login_api(request):
    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    phone = (body.get("phone") or "").strip()
    password = body.get("password") or ""

    if not phone or not password:
        return api_error("phone 和 password 必填", code=4008, status=400)

    try:
        user = FrontendUser.objects.get(phone=phone)
    except FrontendUser.DoesNotExist:
        return api_error("账号或密码错误", code=4009, status=401)

    if not user.status:
        return api_error("账号已被禁用", code=4011, status=403)
    if not user.verify_password(password):
        return api_error("账号或密码错误", code=4012, status=401)

    token = ApiToken.issue_for_user(user)
    return api_response({"token": token.key, "user": serialize_user(user)}, message="登录成功")


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def logout_api(request):
    if request.api_token:
        request.api_token.delete()
    return api_response(message="已退出登录")


@csrf_exempt
@require_api_login
@require_http_methods(["GET", "PATCH"])
def my_profile_api(request):
    if request.method == "PATCH":
        try:
            body = parse_json_body(request)
        except ValueError as exc:
            return api_error(str(exc), code=4001, status=400)

        raw_language = (
            body.get("preferred_language")
            or body.get("preferredLanguage")
            or body.get("language")
            or body.get("locale")
        )
        preferred_language = normalize_preferred_language(raw_language)
        if raw_language is not None and preferred_language is None:
            return api_error("preferred_language 不受支持", code=4013, status=400)

        user = request.api_user
        updated_fields = []
        if preferred_language and user.preferred_language != preferred_language:
            user.preferred_language = preferred_language
            updated_fields.append("preferred_language")
        if updated_fields:
            user.save(update_fields=updated_fields)

    return api_response({"user": serialize_user(request.api_user)})


@csrf_exempt
@require_http_methods(["GET"])
def category_list_api(request):
    categories = TaskCategory.objects.filter(is_active=True).order_by("-sort_order", "id")
    return api_response({"items": [serialize_category(item) for item in categories]})


def _list_tasks(request):
    current_user = get_optional_api_user(request)
    queryset = Task.objects.select_related("publisher", "category").annotate(application_count=Count("applications"))

    status = (request.GET.get("status") or "").strip()
    category_id = (request.GET.get("category_id") or "").strip()
    keyword = (request.GET.get("keyword") or "").strip()
    mine = (request.GET.get("mine") or "").strip()

    if status:
        queryset = queryset.filter(status=status)
    else:
        queryset = queryset.exclude(status=Task.STATUS_DRAFT)

    if category_id:
        try:
            category_id_int = int(category_id)
        except ValueError:
            return api_error("category_id 必须是整数", code=4013, status=400)
        queryset = queryset.filter(category_id=category_id_int)

    binding_platform = (request.GET.get("binding_platform") or "").strip()
    if binding_platform:
        valid_bp = {c[0] for c in Task.BINDING_PLATFORM_CHOICES}
        if binding_platform not in valid_bp:
            return api_error("binding_platform 不合法", code=4044, status=400)
        queryset = queryset.filter(binding_platform=binding_platform)

    if keyword:
        queryset = queryset.filter(Q(title__icontains=keyword) | Q(description__icontains=keyword))

    if mine:
        if not current_user:
            return api_error("mine 查询需要登录", code=4014, status=401)
        if mine == "published":
            queryset = queryset.filter(publisher=current_user)
        elif mine == "applied":
            queryset = queryset.filter(applications__applicant=current_user).distinct()
        else:
            return api_error("mine 仅支持 published 或 applied", code=4015, status=400)

    try:
        page = parse_positive_int(request.GET.get("page", 1), "page")
        page_size = parse_positive_int(request.GET.get("page_size", 20), "page_size")
    except ValueError as exc:
        return api_error(str(exc), code=4016, status=400)
    page_size = min(page_size, 50)

    total = queryset.count()
    offset = (page - 1) * page_size
    items = list(queryset[offset : offset + page_size])

    return api_response(
        {
            "items": [serialize_task(item, current_user=current_user) for item in items],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
            },
        }
    )


def _create_task(request):
    api_uid = getattr(request.api_user, "id", None) if getattr(request, "api_user", None) else None
    if not is_platform_publisher(api_uid):
        return api_error(
            "创建任务须使用平台发布人账号的 Bearer Token（与 settings.TASK_PLATFORM_PUBLISHER_ID 对应）",
            code=4027,
            status=403,
        )

    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    title = (body.get("title") or "").strip()
    description = (body.get("description") or "").strip()
    if not title:
        return api_error("title 不能为空", code=4017, status=400)
    if not description:
        return api_error("description 不能为空", code=4018, status=400)

    try:
        budget = parse_decimal(body.get("budget", "0.00"), "budget")
        applicants_limit = parse_positive_int(body.get("applicants_limit", 1), "applicants_limit")
        deadline = parse_deadline(body.get("deadline"))
    except ValueError as exc:
        return api_error(str(exc), code=4019, status=400)

    category = None
    category_id = body.get("category_id")
    if category_id:
        try:
            category = TaskCategory.objects.get(pk=int(category_id), is_active=True)
        except (TaskCategory.DoesNotExist, ValueError, TypeError):
            return api_error("category_id 无效", code=4020, status=400)

    status = (body.get("status") or Task.STATUS_OPEN).strip()
    if status not in {Task.STATUS_DRAFT, Task.STATUS_OPEN, Task.STATUS_CLOSED, Task.STATUS_COMPLETED, Task.STATUS_IN_PROGRESS}:
        return api_error("status 不合法", code=4021, status=400)

    interaction_type = (body.get("interaction_type") or Task.INTERACTION_NONE).strip()
    valid_interactions = {c[0] for c in Task.INTERACTION_CHOICES}
    if interaction_type not in valid_interactions:
        return api_error("interaction_type 不合法", code=4043, status=400)

    binding_platform = (body.get("binding_platform") or "").strip()
    valid_platforms = {c[0] for c in Task.BINDING_PLATFORM_CHOICES}
    if binding_platform not in valid_platforms:
        return api_error("binding_platform 不合法", code=4044, status=400)

    verification_mode = body.get("verification_mode")
    if verification_mode in (None, ""):
        verification_mode = None
    else:
        verification_mode = str(verification_mode).strip()
        valid_verify = {c[0] for c in Task.VERIFY_CHOICES}
        if verification_mode not in valid_verify:
            return api_error("verification_mode 不合法", code=4045, status=400)

    try:
        interaction_config = parse_interaction_config(body.get("interaction_config"))
    except ValueError as exc:
        return api_error(str(exc), code=4046, status=400)

    reward_usdt = body.get("reward_usdt")
    reward_th_coin = body.get("reward_th_coin")
    if reward_usdt not in (None, ""):
        try:
            reward_usdt = parse_decimal(reward_usdt, "reward_usdt")
        except ValueError as exc:
            return api_error(str(exc), code=4047, status=400)
    else:
        reward_usdt = None
    if reward_th_coin not in (None, ""):
        try:
            reward_th_coin = parse_decimal(reward_th_coin, "reward_th_coin")
        except ValueError as exc:
            return api_error(str(exc), code=4048, status=400)
    else:
        reward_th_coin = None

    is_mandatory = bool(body.get("is_mandatory", False))
    try:
        task_list_order = int(body.get("task_list_order", 0) or 0)
    except (TypeError, ValueError):
        return api_error("task_list_order 须为整数", code=4049, status=400)
    if task_list_order < 0:
        return api_error("task_list_order 不能为负", code=4050, status=400)

    try:
        platform_publisher = get_task_platform_publisher()
    except FrontendUser.DoesNotExist:
        return api_error(
            "平台发布人不存在：请在 settings / 环境变量 TASK_PLATFORM_PUBLISHER_ID 指定有效的前台用户 ID",
            code=4051,
            status=500,
        )
    except ValueError as exc:
        return api_error(str(exc), code=4052, status=500)

    task = Task.objects.create(
        category=category,
        publisher=platform_publisher,
        title=title,
        description=description,
        budget=budget,
        reward_unit=(body.get("reward_unit") or "CNY").strip()[:12] or "CNY",
        deadline=deadline,
        region=(body.get("region") or "").strip() or None,
        applicants_limit=applicants_limit,
        contact_name=(body.get("contact_name") or "").strip() or None,
        contact_phone=(body.get("contact_phone") or "").strip() or None,
        status=status,
        interaction_type=interaction_type,
        binding_platform=binding_platform,
        verification_mode=verification_mode,
        interaction_config=interaction_config,
        is_mandatory=is_mandatory,
        task_list_order=task_list_order,
        reward_usdt=reward_usdt,
        reward_th_coin=reward_th_coin,
    )
    task = Task.objects.select_related("publisher", "category").get(pk=task.pk)
    return api_response({"task": serialize_task(task, current_user=request.api_user, include_contact=True)}, message="任务创建成功")


@csrf_exempt
@require_http_methods(["GET", "POST"])
def task_collection_api(request):
    if request.method == "GET":
        return _list_tasks(request)

    token_value = get_bearer_token(request)
    user, token = resolve_user_by_token(token_value)
    if not user:
        return api_error("发布任务需要先登录", code=4022, status=401)
    request.api_user = user
    request.api_token = token
    return _create_task(request)


@csrf_exempt
@require_http_methods(["GET", "PATCH"])
def task_detail_api(request, task_id):
    current_user = get_optional_api_user(request)
    try:
        task = Task.objects.select_related("publisher", "category").annotate(application_count=Count("applications")).get(pk=task_id)
    except Task.DoesNotExist:
        return api_error("任务不存在", code=4023, status=404)

    if request.method == "GET":
        include_contact = bool(current_user and current_user.id == task.publisher_id)
        return api_response({"task": serialize_task(task, current_user=current_user, include_contact=include_contact)})

    if not current_user:
        return api_error("修改任务需要先登录", code=4024, status=401)
    if current_user.id != task.publisher_id:
        return api_error("仅发布人可以修改任务", code=4025, status=403)

    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    allowed_fields = {
        "title",
        "description",
        "budget",
        "reward_unit",
        "deadline",
        "region",
        "applicants_limit",
        "contact_name",
        "contact_phone",
        "status",
        "category_id",
        "interaction_type",
        "binding_platform",
        "verification_mode",
        "interaction_config",
        "is_mandatory",
        "task_list_order",
        "reward_usdt",
        "reward_th_coin",
    }
    invalid_keys = [key for key in body.keys() if key not in allowed_fields]
    if invalid_keys:
        return api_error(f"不支持的字段: {', '.join(invalid_keys)}", code=4026, status=400)

    try:
        if "title" in body:
            task.title = (body.get("title") or "").strip()
            if not task.title:
                return api_error("title 不能为空", code=4111, status=400)
        if "description" in body:
            task.description = (body.get("description") or "").strip()
            if not task.description:
                return api_error("description 不能为空", code=4028, status=400)
        if "budget" in body:
            task.budget = parse_decimal(body.get("budget"), "budget")
        if "reward_unit" in body:
            reward_unit = (body.get("reward_unit") or "").strip()[:12]
            task.reward_unit = reward_unit or "CNY"
        if "deadline" in body:
            task.deadline = parse_deadline(body.get("deadline"))
        if "region" in body:
            task.region = (body.get("region") or "").strip() or None
        if "applicants_limit" in body:
            task.applicants_limit = parse_positive_int(body.get("applicants_limit"), "applicants_limit")
        if "contact_name" in body:
            task.contact_name = (body.get("contact_name") or "").strip() or None
        if "contact_phone" in body:
            task.contact_phone = (body.get("contact_phone") or "").strip() or None
        if "status" in body:
            status = (body.get("status") or "").strip()
            valid_status = {
                Task.STATUS_DRAFT,
                Task.STATUS_OPEN,
                Task.STATUS_IN_PROGRESS,
                Task.STATUS_COMPLETED,
                Task.STATUS_CLOSED,
            }
            if status not in valid_status:
                return api_error("status 不合法", code=4029, status=400)
            task.status = status
        if "category_id" in body:
            category_id = body.get("category_id")
            if category_id in (None, ""):
                task.category = None
            else:
                try:
                    category = TaskCategory.objects.get(pk=int(category_id), is_active=True)
                except (TaskCategory.DoesNotExist, ValueError, TypeError):
                    return api_error("category_id 无效", code=4030, status=400)
                task.category = category
        if "interaction_type" in body:
            interaction_type = (body.get("interaction_type") or Task.INTERACTION_NONE).strip()
            valid_interactions = {c[0] for c in Task.INTERACTION_CHOICES}
            if interaction_type not in valid_interactions:
                return api_error("interaction_type 不合法", code=4043, status=400)
            task.interaction_type = interaction_type
        if "binding_platform" in body:
            binding_platform = (body.get("binding_platform") or "").strip()
            valid_platforms = {c[0] for c in Task.BINDING_PLATFORM_CHOICES}
            if binding_platform not in valid_platforms:
                return api_error("binding_platform 不合法", code=4044, status=400)
            task.binding_platform = binding_platform
        if "verification_mode" in body:
            vm = body.get("verification_mode")
            if vm in (None, ""):
                task.verification_mode = None
            else:
                vm = str(vm).strip()
                valid_verify = {c[0] for c in Task.VERIFY_CHOICES}
                if vm not in valid_verify:
                    return api_error("verification_mode 不合法", code=4045, status=400)
                task.verification_mode = vm
        if "interaction_config" in body:
            try:
                task.interaction_config = parse_interaction_config(body.get("interaction_config"))
            except ValueError as exc:
                return api_error(str(exc), code=4046, status=400)
        if "is_mandatory" in body:
            task.is_mandatory = bool(body.get("is_mandatory"))
        if "task_list_order" in body:
            try:
                tlo = int(body.get("task_list_order") or 0)
            except (TypeError, ValueError):
                return api_error("task_list_order 须为整数", code=4049, status=400)
            if tlo < 0:
                return api_error("task_list_order 不能为负", code=4050, status=400)
            task.task_list_order = tlo
        if "reward_usdt" in body:
            ru = body.get("reward_usdt")
            if ru in (None, ""):
                task.reward_usdt = None
            else:
                try:
                    task.reward_usdt = parse_decimal(ru, "reward_usdt")
                except ValueError as exc:
                    return api_error(str(exc), code=4047, status=400)
        if "reward_th_coin" in body:
            rtc = body.get("reward_th_coin")
            if rtc in (None, ""):
                task.reward_th_coin = None
            else:
                try:
                    task.reward_th_coin = parse_decimal(rtc, "reward_th_coin")
                except ValueError as exc:
                    return api_error(str(exc), code=4048, status=400)
    except ValueError as exc:
        return api_error(str(exc), code=4031, status=400)

    task.save()
    task = Task.objects.select_related("publisher", "category").annotate(application_count=Count("applications")).get(pk=task.pk)
    return api_response({"task": serialize_task(task, current_user=current_user, include_contact=True)}, message="任务更新成功")


def _task_has_payable_reward_amounts(task: Task) -> bool:
    """与任务记录 annotate 一致：任务上是否配置了正数 USDT/TH 展示奖励。"""
    return (task.reward_usdt is not None and task.reward_usdt > 0) or (
        task.reward_th_coin is not None and task.reward_th_coin > 0
    )


def _task_application_truly_done(application: TaskApplication, task: Task) -> bool:
    """已录用且视为「任务侧已完结」：已发奖，或本任务无应付展示奖励。"""
    if application.status != TaskApplication.STATUS_ACCEPTED:
        return False
    if application.reward_paid_at:
        return True
    if not _task_has_payable_reward_amounts(task):
        return True
    return False


def _reset_task_application_to_pending(
    application: TaskApplication,
    *,
    bound_username: str | None,
    proposal: str | None,
    quoted_price,
) -> None:
    """同一 task+applicant 唯一约束下，允许用户重新走报名/校验流程。"""
    if application.proof_image:
        application.proof_image.delete(save=False)
    application.proof_image = None
    application.self_verified_at = None
    application.decided_at = None
    application.reward_paid_at = None
    application.status = TaskApplication.STATUS_PENDING
    application.bound_username = bound_username or None
    application.proposal = proposal
    application.quoted_price = quoted_price
    application.save(
        update_fields=[
            "proof_image",
            "self_verified_at",
            "decided_at",
            "reward_paid_at",
            "status",
            "bound_username",
            "proposal",
            "quoted_price",
            "updated_at",
        ]
    )


def _accepted_can_reset_to_pending_for_reapply(application: TaskApplication) -> bool:
    """已录用但未结奖：仅当尚未提交任何进度（无自检时间、无截图）时才允许重置为待处理，避免误清「待审核」凭证。"""
    if application.self_verified_at or application.proof_image:
        return False
    return True


def _task_apply_handle_existing_row(request, task, dup, body, bound_username, proposal, quoted_price):
    """
    在事务内处理「同一用户已有一条报名」：幂等 pending、真正已完成、拒绝不可重报、
    已取消/已录用但未产生进度且任务仍 open 时重置为 pending 再同步 body。
    """
    if dup.status == TaskApplication.STATUS_ACCEPTED:
        if _task_application_truly_done(dup, task):
            return api_response(
                {"application": serialize_application(dup, request)},
                message="您已完成该任务",
            )
        if not _accepted_can_reset_to_pending_for_reapply(dup):
            return api_error(
                "您已接取该任务，请继续完成校验或等待审核，无需重复报名。",
                code=4100,
                status=409,
            )
        _reset_task_application_to_pending(
            dup, bound_username=bound_username, proposal=proposal, quoted_price=quoted_price
        )
        dup.refresh_from_db()
        return _task_apply_sync_pending_application(
            request, task, dup, body, bound_username, proposal, quoted_price
        )

    if dup.status == TaskApplication.STATUS_PENDING:
        return _task_apply_sync_pending_application(
            request, task, dup, body, bound_username, proposal, quoted_price
        )

    if dup.status == TaskApplication.STATUS_REJECTED:
        return api_error("该任务报名已被拒绝，无法再次提交", code=4036, status=409)

    if dup.status == TaskApplication.STATUS_CANCELLED:
        vip_resp = _vip_task_application_api_error(task, request.api_user, existing=dup)
        if vip_resp:
            return vip_resp
        if not is_mandatory_no_slot_cap(task):
            if active_taker_count(task) >= effective_applicants_limit(task):
                return api_error("该任务接取人数已满", code=4035, status=400)
        _reset_task_application_to_pending(
            dup, bound_username=bound_username, proposal=proposal, quoted_price=quoted_price
        )
        dup.refresh_from_db()
        return _task_apply_sync_pending_application(
            request, task, dup, body, bound_username, proposal, quoted_price
        )

    return api_error("你已报名该任务", code=4036, status=409)


def _task_apply_sync_pending_application(request, task, existing, body, bound_username, proposal, quoted_price):
    """更新已存在的 pending 报名（调用方须已持有 task/application 行锁或不在并发写路径）。"""
    effective_handle = bound_username or normalize_bound_username_for_task(task, existing.bound_username or "")
    if account_binding_requires_bound_username(task) and not effective_handle:
        if task.binding_platform == Task.BINDING_TWITTER:
            msg = "Twitter 绑定类任务请传 bound_username（不含 @），或与上次报名使用同一用户名"
        else:
            msg = "TikTok 绑定类任务请传 bound_username（用户名或 tiktok.com/@… 链接），或与上次报名一致"
        return api_error(msg, code=4050, status=400)
    update_fields = ["updated_at"]
    if "bound_username" in body:
        existing.bound_username = bound_username
        update_fields.append("bound_username")
    if "proposal" in body:
        existing.proposal = proposal
        update_fields.append("proposal")
    if "quoted_price" in body:
        existing.quoted_price = quoted_price
        update_fields.append("quoted_price")
    if len(update_fields) > 1:
        existing.save(update_fields=update_fields)
    application = TaskApplication.objects.select_related("task", "applicant").get(pk=existing.pk)
    return api_response(
        _serialize_apply_payload(application, request),
        message="已报名（信息已同步，可直接进行校验步骤）",
    )


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def task_apply_api(request, task_id):
    # 与装饰器里 ApiToken.update 等拆开连接，降低同连接事务里叠写触发 1785 的概率
    _close_default_connection_if_safe()
    expire_stale_pending_applications_for_applicant(request.api_user.id, task_id=task_id)

    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    quoted_price = body.get("quoted_price")
    if quoted_price in (None, ""):
        quoted_price = None
    else:
        try:
            quoted_price = parse_decimal(quoted_price, "quoted_price")
        except ValueError as exc:
            return api_error(str(exc), code=4037, status=400)

    bound_username_raw = (body.get("bound_username") or "").strip()
    proposal = (body.get("proposal") or "").strip() or None

    existing = TaskApplication.objects.filter(task_id=task_id, applicant_id=request.api_user.id).first()
    if existing:
        if existing.status == TaskApplication.STATUS_REJECTED:
            return api_error("该任务报名已被拒绝，无法再次提交", code=4036, status=409)

        peek = Task.objects.filter(pk=task_id).only(
            "id", "status", "publisher_id", "reward_usdt", "reward_th_coin"
        ).first()
        if not peek:
            return api_error("任务不存在", code=4032, status=404)

        if existing.status == TaskApplication.STATUS_CANCELLED:
            if peek.status != Task.STATUS_OPEN:
                return api_error(
                    "该任务报名已取消；任务当前不可报名时无法再次报名。",
                    code=4093,
                    status=409,
                )
        elif existing.status == TaskApplication.STATUS_ACCEPTED:
            if _task_application_truly_done(existing, peek):
                application = TaskApplication.objects.select_related("task", "applicant").get(pk=existing.pk)
                return api_response(
                    {"application": serialize_application(application, request)},
                    message="您已完成该任务",
                )
            if peek.status != Task.STATUS_OPEN:
                return api_error(
                    "您曾接取该任务但未完成；当前任务不可报名。若后台将任务重新开放，可再次报名。",
                    code=4097,
                    status=409,
                )
            if not _accepted_can_reset_to_pending_for_reapply(existing):
                return api_error(
                    "您已接取该任务，请继续完成校验或等待审核；任务重新开放后亦不可重复报名。",
                    code=4100,
                    status=409,
                )
            # 已录用、未完成、无自检/截图进度，且任务已重新 open：重置为 pending 并同步报名信息（此前漏 return 会误落 4098）
            with transaction.atomic():
                try:
                    task = Task.objects.select_for_update().get(pk=task_id)
                except Task.DoesNotExist:
                    return api_error("任务不存在", code=4032, status=404)
                if task.publisher_id == request.api_user.id:
                    return api_error("不能报名自己发布的任务", code=4033, status=400)
                if task.status != Task.STATUS_OPEN:
                    return api_error("当前任务不可报名", code=4034, status=400)
                vip_resp = _vip_task_application_api_error(task, request.api_user, existing=existing)
                if vip_resp:
                    return vip_resp
                social_binding_resp = _social_action_binding_api_error(task, request.api_user)
                if social_binding_resp:
                    return social_binding_resp
                locked = TaskApplication.objects.select_for_update().get(pk=existing.pk)
                if locked.status != TaskApplication.STATUS_ACCEPTED:
                    return api_error("报名状态已变更，请刷新页面", code=4090, status=409)
                if _task_application_truly_done(locked, task):
                    application = TaskApplication.objects.select_related("task", "applicant").get(pk=locked.pk)
                    return api_response(
                        {"application": serialize_application(application, request)},
                        message="您已完成该任务",
                    )
                if not _accepted_can_reset_to_pending_for_reapply(locked):
                    return api_error(
                        "您已接取该任务，请继续完成校验或等待审核；任务重新开放后亦不可重复报名。",
                        code=4100,
                        status=409,
                    )
                bound_username = normalize_bound_username_for_task(task, bound_username_raw) or None
                _reset_task_application_to_pending(
                    locked, bound_username=bound_username, proposal=proposal, quoted_price=quoted_price
                )
                locked.refresh_from_db()
                return _task_apply_sync_pending_application(
                    request, task, locked, body, bound_username, proposal, quoted_price
                )
        elif existing.status == TaskApplication.STATUS_PENDING:
            with transaction.atomic():
                try:
                    task = Task.objects.select_for_update().get(pk=task_id)
                except Task.DoesNotExist:
                    return api_error("任务不存在", code=4032, status=404)
                if task.publisher_id == request.api_user.id:
                    return api_error("不能报名自己发布的任务", code=4033, status=400)
                if task.status != Task.STATUS_OPEN:
                    return api_error("当前任务不可报名", code=4034, status=400)
                vip_resp = _vip_task_application_api_error(task, request.api_user, existing=existing)
                if vip_resp:
                    return vip_resp
                social_binding_resp = _social_action_binding_api_error(task, request.api_user)
                if social_binding_resp:
                    return social_binding_resp
                locked = TaskApplication.objects.select_for_update().get(pk=existing.pk)
                if locked.status != TaskApplication.STATUS_PENDING:
                    return api_error("报名状态已变更，请刷新页面", code=4090, status=409)
                bound_username = normalize_bound_username_for_task(task, bound_username_raw) or None
                resp = _task_apply_sync_pending_application(
                    request, task, locked, body, bound_username, proposal, quoted_price
                )
            return resp
        else:
            return api_error("报名状态异常，请刷新后重试", code=4098, status=409)

    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(pk=task_id)
        except Task.DoesNotExist:
            return api_error("任务不存在", code=4032, status=404)

        if task.publisher_id == request.api_user.id:
            return api_error("不能报名自己发布的任务", code=4033, status=400)
        if task.status != Task.STATUS_OPEN:
            return api_error("当前任务不可报名", code=4034, status=400)
        vip_resp = _vip_task_application_api_error(task, request.api_user)
        if vip_resp:
            return vip_resp
        social_binding_resp = _social_action_binding_api_error(task, request.api_user)
        if social_binding_resp:
            return social_binding_resp

        bound_username = normalize_bound_username_for_task(task, bound_username_raw) or None

        dup = (
            TaskApplication.objects.select_for_update()
            .select_related("task", "applicant")
            .filter(task=task, applicant=request.api_user)
            .first()
        )
        if dup:
            return _task_apply_handle_existing_row(
                request, task, dup, body, bound_username, proposal, quoted_price
            )

        if not is_mandatory_no_slot_cap(task):
            if active_taker_count(task) >= effective_applicants_limit(task):
                return api_error("该任务接取人数已满", code=4035, status=400)

        if account_binding_requires_bound_username(task) and not bound_username:
            if task.binding_platform == Task.BINDING_TWITTER:
                msg = "Twitter 绑定类任务请在 body 中传 bound_username（不含 @ 的用户名）"
            else:
                msg = "TikTok 绑定类任务请在 body 中传 bound_username（用户名或含 @用户名的 TikTok 链接）"
            return api_error(msg, code=4050, status=400)

        try:
            application = TaskApplication.objects.create(
                task=task,
                applicant=request.api_user,
                proposal=proposal,
                quoted_price=quoted_price,
                bound_username=bound_username,
            )
        except IntegrityError:
            dup = (
                TaskApplication.objects.select_for_update()
                .select_related("task", "applicant")
                .filter(task_id=task_id, applicant_id=request.api_user.id)
                .first()
            )
            if dup:
                return _task_apply_handle_existing_row(
                    request, task, dup, body, bound_username, proposal, quoted_price
                )
            raise
        new_application_id = application.pk

    maybe_mark_task_completed_when_slots_full(task_id)
    application = TaskApplication.objects.select_related("task", "applicant").get(pk=new_application_id)
    return api_response(_serialize_apply_payload(application, request), message="报名成功")


@csrf_exempt
@require_api_login
@require_http_methods(["GET"])
def task_applications_api(request, task_id):
    try:
        task = Task.objects.get(pk=task_id)
    except Task.DoesNotExist:
        return api_error("任务不存在", code=4038, status=404)
    if task.publisher_id != request.api_user.id:
        return api_error("仅发布人可查看报名列表", code=4039, status=403)

    applications = (
        TaskApplication.objects.select_related("task", "applicant")
        .filter(task=task)
        .order_by("-created_at")
    )
    return api_response(
        {"items": [serialize_application(item, request) for item in applications]}
    )


def _request_payload_dict(request) -> dict:
    payload = {}
    if request.content_type and "json" in request.content_type.lower() and request.body:
        try:
            body = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        if isinstance(body, dict):
            for key, value in body.items():
                payload[str(key)] = "" if value is None else str(value)
    for source in (request.GET, request.POST):
        for key in source:
            payload[key] = source.get(key, "")
    return payload


def _first_payload_value(payload: dict, *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _optional_decimal(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def _mobidea_postback_secret_valid(request, payload: dict) -> bool:
    expected = (getattr(settings, "MOBIDEA_POSTBACK_SECRET", "") or "").strip()
    if not expected:
        return bool(settings.DEBUG)
    received = (
        request.headers.get("X-Mobidea-Secret", "")
        or request.headers.get("X-Postback-Secret", "")
        or payload.get("secret", "")
        or payload.get("postback_secret", "")
    )
    return hmac.compare_digest(str(received).strip(), expected)


def _record_rejected_mobidea_postback(click_id: str, payload: dict, message: str) -> None:
    if not click_id:
        return
    try:
        conversion, created = MobideaConversion.objects.get_or_create(
            click_id=click_id,
            defaults={
                "raw_payload": payload,
                "status": MobideaConversion.STATUS_REJECTED,
                "message": message[:255],
                "processed_at": timezone.now(),
            },
        )
        if not created and conversion.status != MobideaConversion.STATUS_PROCESSED:
            conversion.raw_payload = payload
            conversion.status = MobideaConversion.STATUS_REJECTED
            conversion.message = message[:255]
            conversion.processed_at = timezone.now()
            conversion.save(update_fields=["raw_payload", "status", "message", "processed_at"])
    except OperationalError:
        pass


@csrf_exempt
@require_http_methods(["GET", "POST"])
def mobidea_postback_api(request):
    payload = _request_payload_dict(request)
    if not _mobidea_postback_secret_valid(request, payload):
        return api_error("Mobidea postback secret 校验失败或未配置", code=4500, status=403)

    click_id = _first_payload_value(payload, "click_id", "pub_click_id", "external_id", "EXTERNAL_ID")
    if not click_id:
        return api_error("缺少 click_id", code=4501, status=400)

    application_id = parse_click_id(click_id)
    if not application_id:
        _record_rejected_mobidea_postback(click_id, payload, "invalid click_id signature")
        return api_error("click_id 无效", code=4502, status=400)

    offer_id = _first_payload_value(payload, "offer_id", "app_id", "campaign_id")
    payout = _optional_decimal(_first_payload_value(payload, "money", "payout", "revenue", "MONEY"))
    currency = _first_payload_value(payload, "currency", "payout_currency") or "CNY"
    last_granted = {"granted": False, "usdt": "0", "th_coin": "0"}

    try:
        with transaction.atomic():
            application = (
                TaskApplication.objects.select_for_update()
                .select_related("task", "applicant")
                .get(pk=application_id)
            )
            if not is_mobidea_task(application.task):
                _record_rejected_mobidea_postback(click_id, payload, "application is not a mobidea task")
                return api_error("报名记录不是 Mobidea 任务", code=4503, status=400)
            if not offer_id:
                offer_id = str((application.task.interaction_config or {}).get("mobidea_offer_id") or "").strip()

            try:
                conversion = MobideaConversion.objects.select_for_update().get(click_id=click_id)
                created = False
            except MobideaConversion.DoesNotExist:
                conversion = MobideaConversion.objects.create(
                    click_id=click_id,
                    task_application=application,
                    offer_id=offer_id or None,
                    payout=payout,
                    currency=currency[:12],
                    raw_payload=payload,
                )
                created = True

            if not created and conversion.status == MobideaConversion.STATUS_PROCESSED:
                return api_response(
                    {
                        "processed": False,
                        "duplicate": True,
                        "application_id": conversion.task_application_id,
                    },
                    message="Mobidea 回调已处理，已跳过重复发奖",
                )

            if application.external_provider != MOBIDEA_PROVIDER or application.external_click_id != click_id:
                application.external_provider = MOBIDEA_PROVIDER
                application.external_click_id = click_id
                if not application.external_started_at:
                    application.external_started_at = timezone.now()
                application.save(
                    update_fields=[
                        "external_provider",
                        "external_click_id",
                        "external_started_at",
                        "updated_at",
                    ]
                )

            if application.status != TaskApplication.STATUS_ACCEPTED:
                _auto_accept_after_binding_verify(application)
            last_granted = grant_task_completion_reward(application)

            conversion.task_application = application
            conversion.offer_id = offer_id or conversion.offer_id
            conversion.payout = payout if payout is not None else conversion.payout
            conversion.currency = currency[:12]
            conversion.raw_payload = payload
            conversion.status = MobideaConversion.STATUS_PROCESSED
            conversion.message = "ok"
            conversion.processed_at = timezone.now()
            conversion.save(
                update_fields=[
                    "task_application",
                    "offer_id",
                    "payout",
                    "currency",
                    "raw_payload",
                    "status",
                    "message",
                    "processed_at",
                ]
            )

            def _notify_completion():
                app = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
                send_task_completion_message(app, last_granted)

            transaction.on_commit(_notify_completion)
    except TaskApplication.DoesNotExist:
        _record_rejected_mobidea_postback(click_id, payload, "application not found")
        return api_error("报名记录不存在", code=4504, status=404)
    except OperationalError:
        return api_error("系统繁忙，请稍后重试", code=4505, status=500)

    return api_response(
        {
            "processed": True,
            "duplicate": False,
            "application_id": application_id,
            "last_granted": last_granted,
        },
        message="Mobidea 回调已处理",
    )


@csrf_exempt
@require_api_login
@require_http_methods(["PATCH", "POST"])
def application_review_api(request, application_id):
    try:
        application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    except TaskApplication.DoesNotExist:
        return api_error("报名记录不存在", code=4040, status=404)

    if application.task.publisher_id != request.api_user.id:
        return api_error("仅发布人可处理报名", code=4041, status=403)

    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    target_status = (body.get("status") or "").strip()
    if target_status not in {TaskApplication.STATUS_ACCEPTED, TaskApplication.STATUS_REJECTED, TaskApplication.STATUS_CANCELLED}:
        return api_error("status 必须为 accepted/rejected/cancelled", code=4042, status=400)

    last_holder = {"last_granted": {"granted": False, "usdt": "0", "th_coin": "0"}}
    try:
        with transaction.atomic():
            application = TaskApplication.objects.select_for_update().select_related("task", "applicant").get(
                pk=application_id
            )
            application.status = target_status
            application.decided_at = timezone.now()
            application.save(update_fields=["status", "decided_at", "updated_at"])

            if target_status == TaskApplication.STATUS_ACCEPTED:
                task_ref = application.task
                if is_mandatory_no_slot_cap(task_ref):
                    pass
                elif effective_applicants_limit(task_ref) == 1:
                    TaskApplication.objects.filter(
                        task=task_ref,
                        status=TaskApplication.STATUS_PENDING,
                    ).exclude(pk=application.pk).update(
                        status=TaskApplication.STATUS_REJECTED,
                        decided_at=timezone.now(),
                    )
                after_publisher_accepts_application(task_ref)
                _register_task_reward_on_commit(application_id, last_holder)
    except OperationalError:
        return api_error("系统繁忙，请稍后再试。", code=4094, status=500)

    application = TaskApplication.objects.select_related("task", "applicant").get(pk=application.pk)
    last_granted = last_holder["last_granted"]
    data = {"application": serialize_application(application, request)}
    if target_status == TaskApplication.STATUS_ACCEPTED:
        data["last_granted"] = last_granted
    msg = "报名状态已更新"
    if target_status == TaskApplication.STATUS_ACCEPTED:
        if last_granted.get("reason") == "db_error":
            msg += "；奖励入账失败：" + last_granted.get("message", "")
        elif last_granted.get("granted"):
            msg += f"；已发放奖励 USDT {last_granted.get('usdt')} / TH {last_granted.get('th_coin')}"
        elif last_granted.get("reason") == "no_reward_configured":
            msg += "（本任务未配置奖励金额，钱包无变动）"
    return api_response(data, message=msg)


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def application_upload_proof_api(request, application_id):
    """上传截图凭证：用户提交后保持 pending，由后台在「任务报名」里审核通过/拒绝。"""
    try:
        application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    except TaskApplication.DoesNotExist:
        return api_error("报名记录不存在", code=4322, status=404)

    if application.applicant_id != request.api_user.id:
        return api_error("只能提交自己的任务凭证", code=4323, status=403)

    task = application.task
    if task.verification_mode != Task.VERIFY_SCREENSHOT:
        return api_error("该任务不需要上传截图凭证", code=4324, status=400)
    if task.status in (Task.STATUS_DRAFT, Task.STATUS_CLOSED):
        return api_error("任务已下线，无法提交凭证", code=4325, status=400)
    if task.status == Task.STATUS_COMPLETED and task.deadline and task.deadline < timezone.now():
        return api_error("任务已到期结束，无法提交凭证", code=4326, status=400)
    if application.status != TaskApplication.STATUS_PENDING:
        return api_error("当前报名已处理，无需再次提交凭证", code=4327, status=400)

    uploaded = request.FILES.get("proof_image") or request.FILES.get("image") or request.FILES.get("file")
    if not uploaded:
        return api_error("请上传 proof_image 图片文件", code=4328, status=400)

    if uploaded.size > 10 * 1024 * 1024:
        return api_error("截图不能超过 10MB", code=4329, status=400)

    ext = os.path.splitext(uploaded.name or "")[1].lower()
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    content_type = (getattr(uploaded, "content_type", "") or "").lower()
    if ext not in allowed_exts or not content_type.startswith("image/"):
        return api_error("仅支持 JPG、PNG、WEBP、GIF 图片", code=4330, status=400)

    filename = f"proof_{application.id}_{uuid4().hex}{ext}"
    now = timezone.now()
    try:
        with transaction.atomic():
            application = TaskApplication.objects.select_for_update().select_related("task", "applicant").get(
                pk=application_id
            )
            if application.status != TaskApplication.STATUS_PENDING:
                return api_error("报名状态已变更，请刷新页面", code=4331, status=409)
            if application.proof_image:
                application.proof_image.delete(save=False)
            application.proof_image.save(filename, uploaded, save=False)
            application.self_verified_at = now
            application.save(update_fields=["proof_image", "self_verified_at", "updated_at"])
    except OperationalError:
        return api_error("系统繁忙，请稍后再试。", code=4332, status=500)

    application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    return api_response(
        {"application": serialize_application(application, request)},
        message="凭证已提交，请等待后台审核",
    )


@csrf_exempt
@require_api_login
@require_http_methods(["GET"])
def my_published_tasks_api(request):
    tasks = (
        Task.objects.select_related("publisher", "category")
        .annotate(application_count=Count("applications"))
        .filter(publisher=request.api_user)
        .order_by("-created_at")
    )
    return api_response({"items": [serialize_task(task, current_user=request.api_user, include_contact=True) for task in tasks]})


@csrf_exempt
@require_api_login
@require_http_methods(["GET"])
def my_applied_tasks_api(request):
    applications = (
        TaskApplication.objects.select_related("task", "task__publisher", "task__category", "applicant")
        .filter(applicant=request.api_user)
        .order_by("-created_at")
    )
    items = []
    for application in applications:
        task_data = serialize_task(application.task, current_user=request.api_user)
        task_data["my_application"] = {
            "id": application.id,
            "status": application.status,
            "status_display": application.get_status_display(),
            "proposal": application.proposal,
            "bound_username": application.bound_username,
            "self_verified_at": (
                application.self_verified_at.isoformat() if application.self_verified_at else None
            ),
            "quoted_price": str(application.quoted_price) if application.quoted_price is not None else None,
            "created_at": application.created_at.isoformat(),
        }
        items.append(task_data)
    return api_response({"items": items})


@csrf_exempt
@require_api_login
@require_http_methods(["GET"])
def my_task_records_api(request):
    """
    任务记录列表：与 Mini App「全部 / 进行中 / 审核中 / 已完成 / 已失效」Tab 对齐，分页。
    """
    expire_stale_pending_applications_for_applicant(request.api_user.id)
    raw_tab = (request.GET.get("record_status") or request.GET.get("tab") or RECORD_TAB_ALL).strip().lower()
    tab = RECORD_TAB_ALL if raw_tab in ("", RECORD_TAB_ALL) else raw_tab
    allowed = {
        RECORD_TAB_ALL,
        RECORD_STATUS_IN_PROGRESS,
        RECORD_STATUS_UNDER_REVIEW,
        RECORD_STATUS_COMPLETED,
        RECORD_STATUS_INVALID,
    }
    if tab not in allowed:
        return api_error(
            "record_status 须为 all / in_progress / under_review / completed / invalid（或兼容参数 tab）",
            code=4070,
            status=400,
        )
    try:
        page = parse_positive_int(request.GET.get("page", 1), "page", minimum=1)
        page_size = parse_positive_int(request.GET.get("page_size", 20), "page_size", minimum=1)
    except ValueError as exc:
        return api_error(str(exc), code=4016, status=400)
    page_size = min(page_size, 50)

    qs = _task_applications_queryset_for_record_tabs(request.api_user)
    if tab != RECORD_TAB_ALL:
        qs = qs.filter(record_status=tab)
    total = qs.count()
    offset = (page - 1) * page_size
    rows = list(qs.order_by("-updated_at", "-id")[offset : offset + page_size])
    items = [serialize_task_record_item(a, request.api_user) for a in rows]

    return api_response(
        {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_more": offset + len(items) < total,
            },
        }
    )


def _auto_accept_after_binding_verify(application: TaskApplication) -> None:
    """绑定校验通过后：录用本条（发奖在 on_commit）。必做绑定不关整单、不拒他人；普通任务按 applicants_limit 收尾为已完成。"""
    task = application.task
    now = timezone.now()
    application.self_verified_at = now
    application.status = TaskApplication.STATUS_ACCEPTED
    application.decided_at = now
    application.save(update_fields=["self_verified_at", "status", "decided_at", "updated_at"])
    if is_mandatory_no_slot_cap(task):
        return
    if effective_applicants_limit(task) == 1:
        TaskApplication.objects.filter(task=task, status=TaskApplication.STATUS_PENDING).exclude(
            pk=application.pk
        ).update(status=TaskApplication.STATUS_REJECTED, decided_at=now)
    accepted_count = TaskApplication.objects.filter(task=task, status=TaskApplication.STATUS_ACCEPTED).count()
    if accepted_count >= effective_applicants_limit(task):
        Task.objects.filter(pk=task.pk, status=Task.STATUS_OPEN).update(
            status=Task.STATUS_COMPLETED,
            updated_at=now,
        )
        TaskApplication.objects.filter(task=task, status=TaskApplication.STATUS_PENDING).update(
            status=TaskApplication.STATUS_CANCELLED,
            decided_at=now,
        )


def _register_task_reward_on_commit(application_pk: int, holder: dict) -> None:
    """
    在 atomic 提交后再入账：降低与 django_session(MyISAM) 等同请求 GTID 事务冲突概率。
    holder['last_granted'] 在回调内被赋值。
    """

    def _grant_after_commit():
        # 新开连接，避免与刚结束的请求事务/连接状态在同一 GTID 上下文中叠写。
        _close_default_connection_if_safe()
        try:
            with transaction.atomic():
                app = TaskApplication.objects.select_for_update().select_related("task", "applicant").get(pk=application_pk)
                if app.status == TaskApplication.STATUS_ACCEPTED:
                    holder["last_granted"] = grant_task_completion_reward(app)
                    send_task_completion_message(app, holder["last_granted"])
        except OperationalError:
            holder["last_granted"] = {
                "granted": False,
                "usdt": "0",
                "th_coin": "0",
                "reason": "db_error",
                "message": "奖励入账失败，请稍后再试或联系客服。",
            }

    transaction.on_commit(_grant_after_commit)


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def application_twitter_verify_api(request, application_id):
    """
    推特账号绑定类任务：用户已报名并填写用户名、在站外完成转发/关注后调用。
    服务端用 Twitter API v2（数据库或环境变量中的 Bearer）校验通过后自动录用。
    """
    try:
        application = TaskApplication.objects.select_related("task").get(pk=application_id)
    except TaskApplication.DoesNotExist:
        return api_error("报名记录不存在", code=4080, status=404)

    if application.applicant_id != request.api_user.id:
        return api_error("只能校验自己的报名", code=4081, status=403)

    task = application.task
    if task.interaction_type != Task.INTERACTION_ACCOUNT_BINDING or task.binding_platform != Task.BINDING_TWITTER:
        return api_error("该报名不属于 Twitter 绑定类任务", code=4082, status=400)

    if task.status in (Task.STATUS_DRAFT, Task.STATUS_CLOSED):
        return api_error("任务已下线，无法校验", code=4095, status=400)
    if task.status == Task.STATUS_COMPLETED and task.deadline and task.deadline < timezone.now():
        return api_error("任务已到期结束，无法校验", code=4096, status=400)

    if application.status != TaskApplication.STATUS_PENDING:
        return api_error("当前报名已处理，无需再次校验", code=4083, status=400)
    touch_pending_application_activity(application.id)

    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    body_username = normalize_bound_username_for_task(task, (body.get("bound_username") or "").strip())
    uname = body_username or normalize_bound_username_for_task(task, application.bound_username or "")
    if not uname:
        return api_error("请先填写 Twitter 用户名（报名时可传 bound_username，或本接口 body 再传）", code=4084, status=400)

    if body_username and body_username != normalize_bound_username_for_task(task, application.bound_username or ""):
        application.bound_username = body_username
        application.save(update_fields=["bound_username", "updated_at"])

    cfg = task.interaction_config or {}
    tweet_url = (cfg.get("target_tweet_url") or "").strip()
    tweet_id = extract_tweet_id_from_url(tweet_url) if tweet_url else None
    _rr = cfg.get("require_retweet")
    if _rr is None:
        _rr = bool(tweet_url)
    require_retweet = bool(_rr)
    require_follow = bool(cfg.get("require_follow", False))
    target_follow = normalize_twitter_username((cfg.get("target_follow_username") or "").strip())

    if require_retweet and not tweet_id:
        return api_error("任务要求暂时无法校验，请联系发布方。", code=4091, status=400)
    if require_follow and not target_follow:
        return api_error("任务要求暂时无法校验，请联系发布方。", code=4092, status=400)

    need_twitter_api = (require_retweet and tweet_id) or (require_follow and target_follow)
    use_apify_follow = require_follow and target_follow and apify_twitter_follow_configured()
    use_apify_repost = require_retweet and tweet_id and apify_twitter_repost_configured()
    bearer = get_twitter_bearer_token() if need_twitter_api and not (use_apify_follow and use_apify_repost) else ""

    if need_twitter_api and not (use_apify_follow or use_apify_repost or bearer):
        return api_error("校验服务暂不可用，请稍后再试。", code=4085, status=503)

    if require_retweet and tweet_id:
        if use_apify_repost:
            ok_rt, err = user_retweeted_tweet_via_apify(tweet_url, uname)
            if err and ok_rt is False and err != "并未检测到转发，请先完成转发后再试。":
                status = 503 if apify_twitter_error_is_service_side(err) else 400
                return api_error(err, code=4086, status=status)
        else:
            try:
                ok_rt = user_retweeted_tweet(bearer, tweet_id, uname)
            except ValueError:
                return api_error("暂时无法完成校验，请稍后再试。", code=4086, status=502)
        if not ok_rt:
            return api_error(err if use_apify_repost else "并未检测到转发，请确认已完成转发后再试。", code=4087, status=400)

    if require_follow and target_follow:
        if use_apify_follow:
            ok_f, err = user_follows_username_via_apify(uname, target_follow)
            if err and ok_f is False and err != "并未检测到关注，请先完成关注后再试。":
                status = 503 if apify_twitter_error_is_service_side(err) else 400
                return api_error(err, code=4088, status=status)
        else:
            try:
                ok_f = user_follows_username(bearer, uname, target_follow)
            except ValueError:
                return api_error("暂时无法完成校验，请稍后再试。", code=4088, status=502)
        if not ok_f:
            return api_error(err if use_apify_follow else "并未检测到关注，请先完成关注后再试。", code=4089, status=400)

    last_holder = {"last_granted": {"granted": False, "usdt": "0", "th_coin": "0"}}
    try:
        if need_twitter_api:
            with transaction.atomic():
                application = TaskApplication.objects.select_for_update().get(pk=application.pk)
                if application.status != TaskApplication.STATUS_PENDING:
                    return api_error("报名状态已变更，请刷新页面", code=4090, status=409)
                _auto_accept_after_binding_verify(application)
                _register_task_reward_on_commit(application_id, last_holder)
        else:
            # 未配置需调 Twitter 的校验项时，等价于用户自确认（仍记录完成时间并录用）
            with transaction.atomic():
                application = TaskApplication.objects.select_for_update().get(pk=application.pk)
                if application.status != TaskApplication.STATUS_PENDING:
                    return api_error("报名状态已变更，请刷新页面", code=4090, status=409)
                _auto_accept_after_binding_verify(application)
                _register_task_reward_on_commit(application_id, last_holder)
    except OperationalError:
        return api_error("系统繁忙，请稍后再试。", code=4094, status=500)

    application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    last_granted = last_holder["last_granted"]
    msg = "校验通过，任务已完成"
    if last_granted.get("reason") == "db_error":
        msg += "；奖励入账失败：" + last_granted.get("message", "")
    elif last_granted.get("granted"):
        msg += f"；已发放奖励 USDT {last_granted.get('usdt')} / TH {last_granted.get('th_coin')}"
    elif last_granted.get("reason") == "no_reward_configured":
        msg += "（本任务未配置奖励金额，钱包无变动）"
    return api_response(
        {"application": serialize_application(application, request), "last_granted": last_granted},
        message=msg,
    )


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def application_youtube_verify_api(request, application_id):
    """
    YouTube 账号绑定类任务：用户已在简介/链接区粘贴任务配置的 youtube_proof_link 后调用。
    若任务配置了 youtube_proof_link，会尝试抓取频道 about 页检测该 URL 是否出现（可能受网络或 YouTube 页面结构影响）。
    """
    try:
        application = TaskApplication.objects.select_related("task").get(pk=application_id)
    except TaskApplication.DoesNotExist:
        return api_error("报名记录不存在", code=4100, status=404)

    if application.applicant_id != request.api_user.id:
        return api_error("只能校验自己的报名", code=4101, status=403)

    task = application.task
    if task.interaction_type != Task.INTERACTION_ACCOUNT_BINDING or task.binding_platform != Task.BINDING_YOUTUBE:
        return api_error("该报名不属于 YouTube 绑定类任务", code=4102, status=400)

    if task.status in (Task.STATUS_DRAFT, Task.STATUS_CLOSED):
        return api_error("任务已下线，无法校验", code=4103, status=400)
    if task.status == Task.STATUS_COMPLETED and task.deadline and task.deadline < timezone.now():
        return api_error("任务已到期结束，无法校验", code=4104, status=400)

    if application.status != TaskApplication.STATUS_PENDING:
        return api_error("当前报名已处理，无需再次校验", code=4105, status=400)
    touch_pending_application_activity(application.id)

    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    body_ch = normalize_youtube_channel_identifier((body.get("bound_username") or "").strip())
    ident = body_ch or normalize_youtube_channel_identifier(application.bound_username or "")
    if not ident:
        return api_error(
            "请先填写 YouTube 频道标识（报名时可传 bound_username：@频道名、频道完整 URL、或 channel/UC…）",
            code=4106,
            status=400,
        )

    norm_saved = normalize_youtube_channel_identifier(application.bound_username or "")
    if body_ch and body_ch != norm_saved:
        application.bound_username = body_ch
        application.save(update_fields=["bound_username", "updated_at"])

    cfg = task.interaction_config or {}
    proof_link = (cfg.get("youtube_proof_link") or "").strip()
    if proof_link:
        ok, err = channel_about_contains_proof(ident, proof_link)
        if not ok:
            return api_error(err or "并没有在简介里找到要求填写的内容。", code=4107, status=400)

    last_holder = {"last_granted": {"granted": False, "usdt": "0", "th_coin": "0"}}
    try:
        with transaction.atomic():
            application = TaskApplication.objects.select_for_update().get(pk=application.pk)
            if application.status != TaskApplication.STATUS_PENDING:
                return api_error("报名状态已变更，请刷新页面", code=4108, status=409)
            _auto_accept_after_binding_verify(application)
            _register_task_reward_on_commit(application_id, last_holder)
    except OperationalError:
        return api_error("系统繁忙，请稍后再试。", code=4109, status=500)

    application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    last_granted = last_holder["last_granted"]
    msg = "YouTube 绑定校验通过，任务已完成"
    if last_granted.get("reason") == "db_error":
        msg += "；奖励入账失败：" + last_granted.get("message", "")
    elif last_granted.get("granted"):
        msg += f"；已发放奖励 USDT {last_granted.get('usdt')} / TH {last_granted.get('th_coin')}"
    elif last_granted.get("reason") == "no_reward_configured":
        msg += "（本任务未配置奖励金额，钱包无变动）"
    return api_response(
        {"application": serialize_application(application, request), "last_granted": last_granted},
        message=msg,
    )


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def application_instagram_verify_api(request, application_id):
    """Instagram 账号绑定：任务含简介证明链接时，仅通过 Apify 拉公开资料校验（须配置 APIFY_API_TOKEN）。"""
    try:
        application = TaskApplication.objects.select_related("task").get(pk=application_id)
    except TaskApplication.DoesNotExist:
        return api_error("报名记录不存在", code=4200, status=404)

    if application.applicant_id != request.api_user.id:
        return api_error("只能校验自己的报名", code=4201, status=403)

    task = application.task
    if task.interaction_type != Task.INTERACTION_ACCOUNT_BINDING or task.binding_platform != Task.BINDING_INSTAGRAM:
        return api_error("该报名不属于 Instagram 绑定类任务", code=4202, status=400)

    if task.status in (Task.STATUS_DRAFT, Task.STATUS_CLOSED):
        return api_error("任务已下线，无法校验", code=4203, status=400)
    if task.status == Task.STATUS_COMPLETED and task.deadline and task.deadline < timezone.now():
        return api_error("任务已到期结束，无法校验", code=4204, status=400)

    if application.status != TaskApplication.STATUS_PENDING:
        return api_error("当前报名已处理，无需再次校验", code=4205, status=400)
    touch_pending_application_activity(application.id)

    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    body_ig = normalize_instagram_username((body.get("bound_username") or "").strip())
    ident = body_ig or normalize_instagram_username(application.bound_username or "")
    if not ident:
        return api_error(
            "请先填写 Instagram 用户名（报名时可传 bound_username，或本接口 body 再传；支持 @账号 或 instagram.com/… 链接）",
            code=4206,
            status=400,
        )

    norm_saved = normalize_instagram_username(application.bound_username or "")
    if body_ig and body_ig != norm_saved:
        application.bound_username = body_ig
        application.save(update_fields=["bound_username", "updated_at"])

    cfg = task.interaction_config or {}
    proof_link = (
        (cfg.get("instagram_proof_link") or "").strip()
        or (cfg.get("proof_link") or "").strip()
        or (cfg.get("profile_proof_link") or "").strip()
    )
    if proof_link:
        if not apify_instagram_configured():
            return api_error("校验服务暂不可用，请稍后再试。", code=4210, status=503)
        ok, err = profile_contains_proof_via_apify(ident, proof_link)
        if not ok:
            return api_error(err or "并没有在简介里找到要求填写的内容。", code=4207, status=400)

    last_holder = {"last_granted": {"granted": False, "usdt": "0", "th_coin": "0"}}
    try:
        with transaction.atomic():
            application = TaskApplication.objects.select_for_update().get(pk=application.pk)
            if application.status != TaskApplication.STATUS_PENDING:
                return api_error("报名状态已变更，请刷新页面", code=4208, status=409)
            _auto_accept_after_binding_verify(application)
            _register_task_reward_on_commit(application_id, last_holder)
    except OperationalError:
        return api_error("系统繁忙，请稍后再试。", code=4209, status=500)

    application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    last_granted = last_holder["last_granted"]
    msg = "Instagram 绑定校验通过，任务已完成"
    if last_granted.get("reason") == "db_error":
        msg += "；奖励入账失败：" + last_granted.get("message", "")
    elif last_granted.get("granted"):
        msg += f"；已发放奖励 USDT {last_granted.get('usdt')} / TH {last_granted.get('th_coin')}"
    elif last_granted.get("reason") == "no_reward_configured":
        msg += "（本任务未配置奖励金额，钱包无变动）"
    return api_response(
        {"application": serialize_application(application, request), "last_granted": last_granted},
        message=msg,
    )


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def application_tiktok_verify_api(request, application_id):
    """
    TikTok 账号绑定：用户站外转发指定视频后，传用户名由服务端经 Apify 拉取 Reposts 列表自动校验。
    """
    try:
        application = TaskApplication.objects.select_related("task").get(pk=application_id)
    except TaskApplication.DoesNotExist:
        return api_error("报名记录不存在", code=4220, status=404)

    if application.applicant_id != request.api_user.id:
        return api_error("只能校验自己的报名", code=4221, status=403)

    task = application.task
    if task.interaction_type != Task.INTERACTION_ACCOUNT_BINDING or task.binding_platform != Task.BINDING_TIKTOK:
        return api_error("该报名不属于 TikTok 绑定类任务", code=4222, status=400)

    if task.status in (Task.STATUS_DRAFT, Task.STATUS_CLOSED):
        return api_error("任务已下线，无法校验", code=4223, status=400)
    if task.status == Task.STATUS_COMPLETED and task.deadline and task.deadline < timezone.now():
        return api_error("任务已到期结束，无法校验", code=4224, status=400)

    if application.status != TaskApplication.STATUS_PENDING:
        return api_error("当前报名已处理，无需再次校验", code=4225, status=400)
    touch_pending_application_activity(application.id)

    try:
        body = parse_json_body(request)
    except ValueError as exc:
        return api_error(str(exc), code=4001, status=400)

    body_tt = normalize_bound_username_for_task(task, (body.get("bound_username") or "").strip())
    ident = body_tt or normalize_bound_username_for_task(task, application.bound_username or "")
    if not ident:
        return api_error(
            "请先填写 TikTok 用户名（报名时可传 bound_username，或本接口 body 再传；支持 @账号或 tiktok.com/@… 链接）",
            code=4226,
            status=400,
        )

    norm_saved = normalize_bound_username_for_task(task, application.bound_username or "")
    if body_tt and body_tt != norm_saved:
        application.bound_username = body_tt
        application.save(update_fields=["bound_username", "updated_at"])

    cfg = task.interaction_config or {}
    video_url = (cfg.get("target_video_url") or cfg.get("tiktok_video_url") or "").strip()
    _rr = cfg.get("require_repost")
    if _rr is None:
        _rr = bool(video_url)
    require_repost = bool(_rr)

    if require_repost and not extract_tiktok_video_id_from_url(video_url):
        return api_error("任务要求暂时无法校验，请联系发布方。", code=4227, status=400)

    need_apify = require_repost and bool(video_url)
    if need_apify and not apify_tiktok_configured():
        return api_error("校验服务暂不可用，请稍后再试。", code=4228, status=503)

    if need_apify:
        ok, err = user_reposted_video_via_apify(ident, video_url)
        if not ok:
            status = 503 if apify_tiktok_error_is_service_side(err) else 400
            return api_error(err or "并未检测到转发，请确认已完成转发后再试。", code=4229, status=status)

    last_holder = {"last_granted": {"granted": False, "usdt": "0", "th_coin": "0"}}
    try:
        with transaction.atomic():
            application = TaskApplication.objects.select_for_update().get(pk=application.pk)
            if application.status != TaskApplication.STATUS_PENDING:
                return api_error("报名状态已变更，请刷新页面", code=4230, status=409)
            _auto_accept_after_binding_verify(application)
            _register_task_reward_on_commit(application_id, last_holder)
    except OperationalError:
        return api_error("系统繁忙，请稍后再试。", code=4231, status=500)

    application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    last_granted = last_holder["last_granted"]
    msg = "TikTok 绑定校验通过，任务已完成"
    if last_granted.get("reason") == "db_error":
        msg += "；奖励入账失败：" + last_granted.get("message", "")
    elif last_granted.get("granted"):
        msg += f"；已发放奖励 USDT {last_granted.get('usdt')} / TH {last_granted.get('th_coin')}"
    elif last_granted.get("reason") == "no_reward_configured":
        msg += "（本任务未配置奖励金额，钱包无变动）"
    return api_response(
        {"application": serialize_application(application, request), "last_granted": last_granted},
        message=msg,
    )


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def application_social_action_verify_api(request, application_id):
    """
    关注 / 转发 / 点赞 / 评论类任务：用户完成站外动作后显式点击「我已完成」。
    Twitter / TikTok 转发会按绑定账号做自动校验；其余平台保留用户自确认流转。
    """
    try:
        application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    except TaskApplication.DoesNotExist:
        return api_error("报名记录不存在", code=4300, status=404)

    if application.applicant_id != request.api_user.id:
        return api_error("只能校验自己的报名", code=4301, status=403)

    task = application.task
    if not _interaction_supports_explicit_self_verify(task):
        return api_error("该报名不属于可自行确认完成的社交任务", code=4302, status=400)

    if task.status in (Task.STATUS_DRAFT, Task.STATUS_CLOSED):
        return api_error("任务已下线，无法校验", code=4303, status=400)
    if task.status == Task.STATUS_COMPLETED and task.deadline and task.deadline < timezone.now():
        return api_error("任务已到期结束，无法校验", code=4304, status=400)

    if application.status != TaskApplication.STATUS_PENDING:
        return api_error("当前报名已处理，无需再次校验", code=4305, status=400)
    touch_pending_application_activity(application.id)

    binding_error = _social_action_binding_error(task, request.api_user)
    if binding_error:
        code, msg = binding_error
        return api_error(msg, code=code, status=400)

    bound_app = _accepted_platform_binding_application(request.api_user, task.binding_platform)
    if task.binding_platform == Task.BINDING_TWITTER:
        bound_username = normalize_twitter_username(bound_app.bound_username if bound_app else "")
        ok, code, msg, status = _verify_twitter_social_action(task, bound_username)
        if not ok:
            return api_error(msg, code=code, status=status)
    if task.binding_platform == Task.BINDING_TIKTOK:
        bound_username = normalize_bound_username_for_task(task, bound_app.bound_username if bound_app else "")
        ok, code, msg, status = _verify_tiktok_social_action(task, bound_username)
        if not ok:
            return api_error(msg, code=code, status=status)

    last_holder = {"last_granted": {"granted": False, "usdt": "0", "th_coin": "0"}}
    try:
        with transaction.atomic():
            application = TaskApplication.objects.select_for_update().get(pk=application.pk)
            if application.status != TaskApplication.STATUS_PENDING:
                return api_error("报名状态已变更，请刷新页面", code=4306, status=409)
            _auto_accept_after_binding_verify(application)
            _register_task_reward_on_commit(application_id, last_holder)
    except OperationalError:
        return api_error("系统繁忙，请稍后再试。", code=4307, status=500)

    application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    last_granted = last_holder["last_granted"]
    msg = "校验通过，任务已完成"
    if last_granted.get("reason") == "db_error":
        msg += "；奖励入账失败：" + last_granted.get("message", "")
    elif last_granted.get("granted"):
        msg += f"；已发放奖励 USDT {last_granted.get('usdt')} / TH {last_granted.get('th_coin')}"
    elif last_granted.get("reason") == "no_reward_configured":
        msg += "（本任务未配置奖励金额，钱包无变动）"
    return api_response(
        {"application": serialize_application(application, request), "last_granted": last_granted},
        message=msg,
    )


@csrf_exempt
@require_api_login
@require_http_methods(["POST"])
def application_telegram_group_verify_api(request, application_id):
    """
    加入社群（Telegram）：用户已入群后调用；服务端用 Bot getChatMember 校验当前用户 telegram_id 是否在配置的群内，通过后自动录用。
    """
    try:
        application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    except TaskApplication.DoesNotExist:
        return api_error("报名记录不存在", code=4310, status=404)

    if application.applicant_id != request.api_user.id:
        return api_error("只能校验自己的报名", code=4311, status=403)

    task = application.task
    if task.interaction_type != Task.INTERACTION_JOIN_COMMUNITY:
        return api_error("该报名不属于加入社群类任务", code=4312, status=400)

    if not _join_community_telegram_verify_enabled(task):
        return api_error("本任务未开启入群自动校验。", code=4313, status=400)

    if task.status in (Task.STATUS_DRAFT, Task.STATUS_CLOSED):
        return api_error("任务已下线，无法校验", code=4314, status=400)
    if task.status == Task.STATUS_COMPLETED and task.deadline and task.deadline < timezone.now():
        return api_error("任务已到期结束，无法校验", code=4315, status=400)

    if application.status != TaskApplication.STATUS_PENDING:
        return api_error("当前报名已处理，无需再次校验", code=4316, status=400)
    touch_pending_application_activity(application.id)

    bot_token = get_telegram_bot_token()
    if not bot_token:
        return api_error("校验服务暂不可用，请稍后再试。", code=4317, status=503)

    tid = request.api_user.telegram_id
    if not tid:
        return api_error("请先使用 Telegram 登录本应用，以便校验您是否已入群。", code=4318, status=400)

    cfg = task.interaction_config or {}
    chat_id = extract_telegram_chat_id_from_config(cfg)
    if not chat_id:
        return api_error(
            "本任务未配置可校验的频道/群标识。请填写 telegram_chat_id / telegram_group_id 为 @频道用户名 或 -100… 数字 ID，不要填邀请链接。",
            code=4333,
            status=400,
        )

    ok, err = user_is_member_of_chat(bot_token, chat_id, int(tid))
    if not ok:
        return api_error(err or "未检测到您已加入该群组，请先入群后再试。", code=4319, status=400)

    last_holder = {"last_granted": {"granted": False, "usdt": "0", "th_coin": "0"}}
    try:
        with transaction.atomic():
            application = TaskApplication.objects.select_for_update().get(pk=application.pk)
            if application.status != TaskApplication.STATUS_PENDING:
                return api_error("报名状态已变更，请刷新页面", code=4320, status=409)
            _auto_accept_after_binding_verify(application)
            _register_task_reward_on_commit(application_id, last_holder)
    except OperationalError:
        return api_error("系统繁忙，请稍后再试。", code=4321, status=500)

    application = TaskApplication.objects.select_related("task", "applicant").get(pk=application_id)
    last_granted = last_holder["last_granted"]
    msg = "入群校验通过，任务已完成"
    if last_granted.get("reason") == "db_error":
        msg += "；" + str(last_granted.get("message", ""))
    elif last_granted.get("granted"):
        msg += f"；已发放奖励 USDT {last_granted.get('usdt')} / TH {last_granted.get('th_coin')}"
    elif last_granted.get("reason") == "no_reward_configured":
        msg += "（本任务未配置奖励金额，钱包无变动）"
    return api_response(
        {"application": serialize_application(application, request), "last_granted": last_granted},
        message=msg,
    )


@csrf_exempt
@require_http_methods(["GET"])
def docs_api(request):
    from .api_endpoints import get_endpoints_for_public_json

    endpoints = get_endpoints_for_public_json()
    return api_response(
        {
            "service": "taskhub-api",
            "auth": "Authorization: Bearer <token>",
            "endpoints": endpoints,
            "doc_file": "docs/taskhub_api.md",
            "doc_page_url": "/docs/taskhub-api/",
            "doc_sync_command": "python manage.py sync_taskhub_api_docs",
        }
    )
