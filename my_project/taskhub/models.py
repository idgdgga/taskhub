import secrets
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from users.models import FrontendUser


class TaskCategory(models.Model):
    name = models.CharField(max_length=50, unique=True, verbose_name="分类名称", db_comment="任务分类名称")
    slug = models.SlugField(max_length=60, unique=True, verbose_name="分类标识", db_comment="任务分类唯一标识")
    description = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name="分类描述",
        db_comment="分类简介",
    )
    sort_order = models.IntegerField(default=0, verbose_name="排序权重", db_comment="数值越大越靠前")
    is_active = models.BooleanField(default=True, verbose_name="是否启用", db_comment="前台是否展示")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间", db_comment="分类创建时间")

    class Meta:
        db_table = "task_category"
        verbose_name = "任务分类"
        verbose_name_plural = verbose_name
        ordering = ("-sort_order", "name")

    def __str__(self):
        return self.name


class Task(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_OPEN = "open"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "草稿"),
        (STATUS_OPEN, "可报名"),
        (STATUS_IN_PROGRESS, "进行中"),
        (STATUS_COMPLETED, "已完成"),
        (STATUS_CLOSED, "已关闭"),
    )

    # —— 必做任务：交互类型（与分类独立，按单条任务配置）——
    INTERACTION_NONE = "none"
    INTERACTION_ACCOUNT_BINDING = "account_binding"
    INTERACTION_FOLLOW = "follow"
    INTERACTION_REPOST = "repost"
    INTERACTION_LIKE = "like"
    INTERACTION_COMMENT = "comment"
    INTERACTION_WATCH_VIDEO = "watch_video"
    INTERACTION_EXTERNAL_VOTE = "external_vote"
    INTERACTION_JOIN_COMMUNITY = "join_community"
    INTERACTION_SCREENSHOT_PROOF = "screenshot_proof"
    INTERACTION_CPA_OFFER = "cpa_offer"
    INTERACTION_CHOICES = (
        (INTERACTION_NONE, "无（不按下面规则校验）"),
        (INTERACTION_ACCOUNT_BINDING, "账号绑定（首页「绑定 Twitter/TikTok…」卡片）"),
        (INTERACTION_JOIN_COMMUNITY, "加入社群（如 Telegram 入群/频道）"),
        (INTERACTION_FOLLOW, "关注"),
        (INTERACTION_REPOST, "转发 / Repost / Retweet"),
        (INTERACTION_LIKE, "点赞"),
        (INTERACTION_COMMENT, "评论"),
        (INTERACTION_WATCH_VIDEO, "观看视频"),
        (INTERACTION_EXTERNAL_VOTE, "外部网页投票"),
        (INTERACTION_SCREENSHOT_PROOF, "上传截图审核"),
        (INTERACTION_CPA_OFFER, "CPA 外部任务（Postback 自动确认）"),
    )

    BINDING_PLATFORM_NONE = ""
    BINDING_TWITTER = "twitter"
    BINDING_YOUTUBE = "youtube"
    BINDING_INSTAGRAM = "instagram"
    BINDING_TIKTOK = "tiktok"
    BINDING_FACEBOOK = "facebook"
    BINDING_TELEGRAM = "telegram"
    BINDING_PLATFORM_CHOICES = (
        (BINDING_PLATFORM_NONE, "—"),
        (BINDING_TWITTER, "Twitter / X"),
        (BINDING_YOUTUBE, "YouTube"),
        (BINDING_INSTAGRAM, "Instagram"),
        (BINDING_TIKTOK, "TikTok"),
        (BINDING_FACEBOOK, "Facebook"),
        (BINDING_TELEGRAM, "Telegram"),
    )

    # 校验方式：推特绑定=填用户名+（可选）转发后用户点完成；YouTube=简介留指定链接；关注/评论=用户点完成；看视频/投票=截图审核
    VERIFY_USER_SELF = "user_self_confirm"
    VERIFY_PROFILE_LINK = "profile_link_proof"
    VERIFY_SCREENSHOT = "screenshot_review"
    VERIFY_POSTBACK = "postback"
    VERIFY_CHOICES = (
        (VERIFY_USER_SELF, "用户自行确认完成"),
        (VERIFY_PROFILE_LINK, "简介/频道留指定链接证明"),
        (VERIFY_SCREENSHOT, "上传截图待审核"),
        (VERIFY_POSTBACK, "第三方 Postback 自动确认"),
    )

    category = models.ForeignKey(
        TaskCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="任务分类",
        db_comment="任务所属分类",
    )
    publisher = models.ForeignKey(
        FrontendUser,
        on_delete=models.CASCADE,
        related_name="published_tasks",
        verbose_name="发布人",
        db_comment="任务发布用户ID",
    )
    title = models.CharField(max_length=200, verbose_name="任务标题", db_comment="任务标题")
    description = models.TextField(verbose_name="任务描述", db_comment="任务详情")
    budget = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name="预算金额",
        db_comment="任务预算金额",
    )
    reward_unit = models.CharField(max_length=12, default="CNY", verbose_name="币种", db_comment="预算币种")
    deadline = models.DateTimeField(blank=True, null=True, verbose_name="截止时间", db_comment="任务报名或完成截止时间")
    region = models.CharField(max_length=120, blank=True, null=True, verbose_name="任务地区", db_comment="任务执行地区")
    applicants_limit = models.PositiveIntegerField(default=1, verbose_name="需求人数", db_comment="任务可录用人数")
    contact_name = models.CharField(max_length=50, blank=True, null=True, verbose_name="联系人", db_comment="联系姓名")
    contact_phone = models.CharField(max_length=30, blank=True, null=True, verbose_name="联系电话", db_comment="联系方式")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_OPEN,
        verbose_name="任务状态",
        db_comment="任务当前状态",
    )
    interaction_type = models.CharField(
        max_length=32,
        choices=INTERACTION_CHOICES,
        default=INTERACTION_NONE,
        verbose_name="必做任务类型",
        db_comment="账号绑定/关注/评论/看视频/外站投票等",
    )
    binding_platform = models.CharField(
        max_length=20,
        choices=BINDING_PLATFORM_CHOICES,
        default=BINDING_PLATFORM_NONE,
        blank=True,
        verbose_name="目标平台",
        db_comment="账号绑定 / 关注 / 点赞类任务使用；决定前台图标与平台校验文案",
    )
    verification_mode = models.CharField(
        max_length=32,
        choices=VERIFY_CHOICES,
        blank=True,
        null=True,
        verbose_name="校验方式",
        db_comment="由业务推导，也可在后台手动改",
    )
    interaction_config = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="交互配置 JSON",
        db_comment="如 target_tweet_url、require_retweet、youtube_proof_link、invite_link 等",
    )
    is_mandatory = models.BooleanField(
        default=False,
        verbose_name="首页显示「必做」角标",
        db_comment="对应 TaskFlow 卡片右上角红色必做丝带",
    )
    is_vip_exclusive = models.BooleanField(
        default=False,
        verbose_name="VIP 任务专区",
        db_comment="勾选后仅在前台 VIP 任务专区展示，并按会员等级限制每日可接次数",
    )
    task_list_order = models.PositiveIntegerField(
        default=0,
        verbose_name="首页列表排序权重",
        db_comment="数值越大越靠前，用于必做任务区排序",
    )
    virtual_application_count = models.PositiveIntegerField(
        default=0,
        verbose_name="虚拟参与人数",
        db_comment="基础虚拟参与人数，仅用于前台任务列表展示，会叠加真实报名数与自动增长数",
    )
    virtual_hourly_growth_min = models.PositiveIntegerField(
        default=0,
        verbose_name="每小时虚拟增长最小值",
        db_comment="启用自动增长后，每整小时随机增加的最小人数；0 表示不启用",
    )
    virtual_hourly_growth_max = models.PositiveIntegerField(
        default=0,
        verbose_name="每小时虚拟增长最大值",
        db_comment="启用自动增长后，每整小时随机增加的最大人数；需大于等于最小值",
    )
    virtual_auto_increment_count = models.PositiveIntegerField(
        default=0,
        editable=False,
        verbose_name="自动增长累计人数",
        db_comment="系统按小时自动累加的虚拟参与人数",
    )
    virtual_growth_last_at = models.DateTimeField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="虚拟增长上次结算时间",
        db_comment="系统最近一次结算虚拟参与人数增长的时间",
    )
    reward_usdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name="展示奖励 USDT",
        db_comment="卡片上 +0.05 USDT 等，可与 budget 独立",
    )
    reward_th_coin = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name="展示奖励 TH",
        db_comment="卡片上 +2 TH 等",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间", db_comment="任务创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间", db_comment="任务更新时间")

    class Meta:
        db_table = "task_job"
        verbose_name = "任务（发布单 / 首页必做卡片）"
        verbose_name_plural = verbose_name
        ordering = ("-task_list_order", "-created_at")

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    def display_application_count(self, real_count: int | None = None) -> int:
        try:
            base = int(real_count if real_count is not None else 0)
        except (TypeError, ValueError):
            base = 0
        return max(0, base) + self.display_virtual_application_count()

    def display_virtual_application_count(self) -> int:
        try:
            virtual = int(self.virtual_application_count or 0)
        except (TypeError, ValueError):
            virtual = 0
        try:
            auto_growth = int(self.virtual_auto_increment_count or 0)
        except (TypeError, ValueError):
            auto_growth = 0
        return max(0, virtual) + max(0, auto_growth)

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.interaction_type in {
            self.INTERACTION_ACCOUNT_BINDING,
            self.INTERACTION_FOLLOW,
            self.INTERACTION_REPOST,
            self.INTERACTION_LIKE,
        } and not self.binding_platform:
            label = {
                self.INTERACTION_ACCOUNT_BINDING: "账号绑定",
                self.INTERACTION_FOLLOW: "关注",
                self.INTERACTION_REPOST: "转发",
                self.INTERACTION_LIKE: "点赞",
            }.get(self.interaction_type, "当前玩法")
            raise ValidationError({"binding_platform": f"必做类型为「{label}」时必须选择目标平台。"})
        if self.interaction_type == self.INTERACTION_JOIN_COMMUNITY:
            link = (self.interaction_config or {}).get("invite_link") or (self.interaction_config or {}).get(
                "telegram_invite_link"
            )
            if not link or not str(link).strip():
                raise ValidationError(
                    {
                        "interaction_config": "「加入社群」类任务请在 JSON 里填写 invite_link（或 telegram_invite_link），即用户要打开的 Telegram 链接。"
                    }
                )
        if self.virtual_hourly_growth_max < self.virtual_hourly_growth_min:
            raise ValidationError({"virtual_hourly_growth_max": "每小时虚拟增长最大值不能小于最小值。"})

    def save(self, *args, **kwargs):
        if self.interaction_type == self.INTERACTION_JOIN_COMMUNITY:
            self.binding_platform = self.BINDING_PLATFORM_NONE
        elif self.interaction_type not in {
            self.INTERACTION_ACCOUNT_BINDING,
            self.INTERACTION_FOLLOW,
            self.INTERACTION_REPOST,
            self.INTERACTION_LIKE,
        }:
            self.binding_platform = self.BINDING_PLATFORM_NONE
        if self.interaction_type == self.INTERACTION_NONE:
            self.verification_mode = None
            self.interaction_config = {}
        elif not self.verification_mode:
            self.verification_mode = self._default_verification_mode()
        super().save(*args, **kwargs)

    def _default_verification_mode(self):
        if self.interaction_type == self.INTERACTION_ACCOUNT_BINDING:
            if self.binding_platform == self.BINDING_TWITTER:
                return self.VERIFY_USER_SELF
            if self.binding_platform == self.BINDING_TELEGRAM:
                return self.VERIFY_USER_SELF
            if self.binding_platform == self.BINDING_TIKTOK:
                return self.VERIFY_USER_SELF
            if self.binding_platform in {
                self.BINDING_YOUTUBE,
                self.BINDING_INSTAGRAM,
                self.BINDING_FACEBOOK,
            }:
                return self.VERIFY_PROFILE_LINK
            return self.VERIFY_USER_SELF
        if self.interaction_type in (
            self.INTERACTION_FOLLOW,
            self.INTERACTION_REPOST,
            self.INTERACTION_LIKE,
            self.INTERACTION_COMMENT,
            self.INTERACTION_JOIN_COMMUNITY,
        ):
            return self.VERIFY_USER_SELF
        if self.interaction_type in (
            self.INTERACTION_WATCH_VIDEO,
            self.INTERACTION_EXTERNAL_VOTE,
            self.INTERACTION_SCREENSHOT_PROOF,
        ):
            return self.VERIFY_SCREENSHOT
        if self.interaction_type == self.INTERACTION_CPA_OFFER:
            return self.VERIFY_POSTBACK
        return self.VERIFY_USER_SELF


class TaskApplication(models.Model):
    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_PENDING, "待处理"),
        (STATUS_ACCEPTED, "已录用"),
        (STATUS_REJECTED, "已拒绝"),
        (STATUS_CANCELLED, "已取消"),
    )

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="applications",
        verbose_name="报名任务",
        db_comment="被报名的任务ID",
    )
    applicant = models.ForeignKey(
        FrontendUser,
        on_delete=models.CASCADE,
        related_name="task_applications",
        verbose_name="报名用户",
        db_comment="报名用户ID",
    )
    proposal = models.TextField(blank=True, null=True, verbose_name="报名说明", db_comment="报名留言或补充说明")
    bound_username = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        verbose_name="绑定账号用户名",
        db_comment="如推特 @handle，用于账号绑定类任务",
    )
    proof_image = models.ImageField(
        upload_to="task_applications/proof/%Y/%m/",
        blank=True,
        null=True,
        verbose_name="完成凭证截图",
        db_comment="看视频、外站投票等需截图审核时使用",
    )
    external_provider = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        db_index=True,
        verbose_name="外部任务来源",
        db_comment="如 mobidea；CPA/Postback 任务使用",
    )
    external_click_id = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        unique=True,
        verbose_name="外部点击 ID",
        db_comment="传给第三方的唯一 click_id/pub_click_id，用于回调匹配报名",
    )
    external_started_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="外部任务打开时间",
        db_comment="用户领取后首次生成第三方跳转链接的时间",
    )
    self_verified_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="用户标记完成时间",
        db_comment="用户点击验证完成的时间",
    )
    quoted_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name="报价",
        db_comment="报名时的报价",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        verbose_name="报名状态",
        db_comment="报名处理状态",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="报名时间", db_comment="报名创建时间")
    decided_at = models.DateTimeField(blank=True, null=True, verbose_name="处理时间", db_comment="发布人处理报名的时间")
    reward_paid_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="任务奖励已发放时间",
        db_comment="按任务 reward_usdt/reward_th_coin 入账后写入，防重复发奖",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间", db_comment="更新时间")

    class Meta:
        db_table = "task_application"
        verbose_name = "任务报名"
        verbose_name_plural = verbose_name
        unique_together = ("task", "applicant")
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.applicant.username} -> {self.task.title}"


class MobideaConversion(models.Model):
    STATUS_RECEIVED = "received"
    STATUS_PROCESSED = "processed"
    STATUS_DUPLICATE = "duplicate"
    STATUS_REJECTED = "rejected"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_RECEIVED, "已接收"),
        (STATUS_PROCESSED, "已处理并发奖"),
        (STATUS_DUPLICATE, "重复回调"),
        (STATUS_REJECTED, "已拒绝"),
        (STATUS_FAILED, "处理失败"),
    )

    task_application = models.ForeignKey(
        TaskApplication,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mobidea_conversions",
        verbose_name="关联报名",
        db_comment="通过 click_id 匹配到的 TaskApplication",
    )
    click_id = models.CharField(
        max_length=128,
        unique=True,
        verbose_name="Click ID",
        db_comment="Mobidea 回传的 {{EXTERNAL_ID}} / pub_click_id",
    )
    offer_id = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        verbose_name="Mobidea Offer ID",
        db_comment="Mobidea offer_id",
    )
    payout = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        blank=True,
        null=True,
        verbose_name="Mobidea 收益",
        db_comment="Mobidea 回传的 {{MONEY}}",
    )
    currency = models.CharField(
        max_length=12,
        default="CNY",
        verbose_name="币种",
        db_comment="Mobidea payout 币种",
    )
    raw_payload = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="原始回调",
        db_comment="Mobidea postback 参数",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_RECEIVED,
        verbose_name="处理状态",
        db_comment="Mobidea 回调处理状态",
    )
    message = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name="处理说明",
        db_comment="错误或重复说明",
    )
    received_at = models.DateTimeField(auto_now_add=True, verbose_name="接收时间", db_comment="回调接收时间")
    processed_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="处理时间",
        db_comment="处理完成时间",
    )

    class Meta:
        db_table = "taskhub_mobidea_conversion"
        verbose_name = "Mobidea 转化回调"
        verbose_name_plural = verbose_name
        ordering = ("-received_at",)

    def __str__(self):
        return self.click_id


class TaskCompletionRecord(TaskApplication):
    """
    与 task_application 同表；后台「任务完成记录」仅展示 status=已录用 的报名，
    便于运营查看完成/发奖情况，勿单独建物理表。
    """

    class Meta:
        proxy = True
        verbose_name = "任务完成记录"
        verbose_name_plural = verbose_name


class ScreenshotProofReview(TaskApplication):
    """
    与 task_application 同表；后台「截图任务审核」仅展示已上传截图的截图审核任务，
    方便运营集中处理用户提交的凭证。
    """

    class Meta:
        proxy = True
        verbose_name = "截图任务审核"
        verbose_name_plural = verbose_name


class CheckInConfig(models.Model):
    """全局签到规则（后台仅维护一条记录）。"""

    daily_reward_usdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0"),
        verbose_name="每日签到奖励 USDT",
        db_comment="成功签到一次增加的 USDT（可为 0）",
    )
    daily_reward_th_coin = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0"),
        verbose_name="每日签到奖励 TH Coin",
        db_comment="成功签到一次增加的 TH Coin（可为 0）",
    )
    makeup_cost_th_coin = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0"),
        verbose_name="补签消耗 TH Coin",
        db_comment="每次补签从 TH Coin 扣除的数量；为 0 表示不消耗",
    )
    weekly_makeup_limit = models.PositiveIntegerField(
        default=3,
        verbose_name="每周补签次数上限",
        db_comment="自然周内最多允许补签次数",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "task_check_in_config"
        verbose_name = "签到参数配置"
        verbose_name_plural = verbose_name

    def __str__(self):
        return "签到参数（全局）"

    @classmethod
    def get(cls):
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create()
        return obj


class ReferralRewardConfig(models.Model):
    """邀请返佣单例配置。"""

    direct_invite_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0.20"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        verbose_name="一级任务分成比例",
        db_comment="一级下级完成任务后，按其实际到账 USDT 任务奖励乘以该比例给上级发放推荐奖励；0.20 = 20 percent",
    )
    second_task_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0.10"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        verbose_name="二级任务分成比例",
        db_comment="二级下级完成任务后，按其实际到账 USDT 任务奖励乘以该比例给上二级发放奖励；0.10 = 10 percent",
    )
    direct_recharge_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0.10"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        verbose_name="一级充值佣金比例",
        db_comment="一级下级充值后，按充值 USDT 金额乘以该比例给上级发放奖励；0.10 = 10 percent",
    )
    second_recharge_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0.05"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        verbose_name="二级充值佣金比例",
        db_comment="二级下级充值后，按充值 USDT 金额乘以该比例给上二级发放奖励；0.05 = 5 percent",
    )
    activity_title = models.CharField(
        max_length=128,
        default="关于任务邀请好友拉新活动会员等级",
        verbose_name="活动标题",
        db_comment="前台/机器人展示的邀请拉新活动标题",
    )
    activity_intro = models.TextField(
        blank=True,
        default="邀请好友加入 TaskHub，完成任务、充值会员与团队成长都可按后台配置获得奖励。",
        verbose_name="活动简介",
        db_comment="前台/机器人展示的活动简介，可按运营口径修改",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "task_referral_reward_config"
        verbose_name = "邀请返佣配置"
        verbose_name_plural = verbose_name

    def __str__(self):
        pct = (self.direct_invite_rate * Decimal("100")).quantize(Decimal("0.01"))
        return f"邀请返佣配置（一级任务 {pct}%）"

    @classmethod
    def get(cls):
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create()
        return obj


class PlatformStatsDisplayConfig(models.Model):
    """首页排行榜统计配置：4项统计支持虚拟叠加，在线人数走实时活跃。"""

    total_tasks_virtual_base = models.PositiveIntegerField(
        default=0,
        verbose_name="任务总数虚拟基础值",
        db_comment="叠加到真实任务总数上的基础虚拟值",
    )
    total_tasks_hourly_growth_min = models.PositiveIntegerField(
        default=0,
        verbose_name="任务总数每小时增长最小值",
        db_comment="每整小时随机增加的最小任务总数虚拟值",
    )
    total_tasks_hourly_growth_max = models.PositiveIntegerField(
        default=0,
        verbose_name="任务总数每小时增长最大值",
        db_comment="每整小时随机增加的最大任务总数虚拟值",
    )
    total_tasks_virtual_auto_increment = models.PositiveIntegerField(
        default=0,
        editable=False,
        verbose_name="任务总数自动增长累计值",
        db_comment="系统按小时累计增长的任务总数虚拟值",
    )

    total_rewards_usdt_virtual_base = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name="总发放奖励虚拟基础值（USDT）",
        db_comment="叠加到真实总发放奖励上的基础虚拟值",
    )
    total_rewards_usdt_hourly_growth_min = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name="总发放奖励每小时增长最小值（USDT）",
        db_comment="每整小时随机增加的最小总发放奖励虚拟值",
    )
    total_rewards_usdt_hourly_growth_max = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name="总发放奖励每小时增长最大值（USDT）",
        db_comment="每整小时随机增加的最大总发放奖励虚拟值",
    )
    total_rewards_usdt_virtual_auto_increment = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        editable=False,
        verbose_name="总发放奖励自动增长累计值（USDT）",
        db_comment="系统按小时累计增长的总发放奖励虚拟值",
    )

    total_users_virtual_base = models.PositiveIntegerField(
        default=0,
        verbose_name="总用户数虚拟基础值",
        db_comment="叠加到真实总用户数上的基础虚拟值",
    )
    total_users_hourly_growth_min = models.PositiveIntegerField(
        default=0,
        verbose_name="总用户数每小时增长最小值",
        db_comment="每整小时随机增加的最小总用户数虚拟值",
    )
    total_users_hourly_growth_max = models.PositiveIntegerField(
        default=0,
        verbose_name="总用户数每小时增长最大值",
        db_comment="每整小时随机增加的最大总用户数虚拟值",
    )
    total_users_virtual_auto_increment = models.PositiveIntegerField(
        default=0,
        editable=False,
        verbose_name="总用户数自动增长累计值",
        db_comment="系统按小时累计增长的总用户数虚拟值",
    )

    operating_days_virtual_base = models.PositiveIntegerField(
        default=0,
        verbose_name="运营天数虚拟基础值",
        db_comment="叠加到真实运营天数上的基础虚拟值",
    )
    operating_days_hourly_growth_min = models.PositiveIntegerField(
        default=0,
        verbose_name="运营天数每小时增长最小值",
        db_comment="每整小时随机增加的最小运营天数虚拟值",
    )
    operating_days_hourly_growth_max = models.PositiveIntegerField(
        default=0,
        verbose_name="运营天数每小时增长最大值",
        db_comment="每整小时随机增加的最大运营天数虚拟值",
    )
    operating_days_virtual_auto_increment = models.PositiveIntegerField(
        default=0,
        editable=False,
        verbose_name="运营天数自动增长累计值",
        db_comment="系统按小时累计增长的运营天数虚拟值",
    )

    virtual_growth_last_at = models.DateTimeField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="统计虚拟增长上次结算时间",
        db_comment="系统最近一次按小时结算统计虚拟增长的时间",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "task_platform_stats_display_config"
        verbose_name = "首页排行榜统计配置"
        verbose_name_plural = verbose_name

    def __str__(self):
        return "首页排行榜统计配置"

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        checks = (
            ("total_tasks_hourly_growth_min", "total_tasks_hourly_growth_max", "任务总数"),
            ("total_rewards_usdt_hourly_growth_min", "total_rewards_usdt_hourly_growth_max", "总发放奖励"),
            ("total_users_hourly_growth_min", "total_users_hourly_growth_max", "总用户数"),
            ("operating_days_hourly_growth_min", "operating_days_hourly_growth_max", "运营天数"),
        )
        errors = {}
        for min_field, max_field, label in checks:
            min_value = getattr(self, min_field, 0) or 0
            max_value = getattr(self, max_field, 0) or 0
            if max_value < min_value:
                errors[max_field] = f"{label}每小时增长最大值不能小于最小值。"
        if errors:
            raise ValidationError(errors)

    @classmethod
    def get(cls):
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create()
        return obj


class MembershipLevelConfig(models.Model):
    """会员等级活动规则：费用、VIP任务权限、每日领取上限与提现手续费。"""

    level = models.PositiveSmallIntegerField(
        unique=True,
        verbose_name="等级数值",
        db_comment="与 FrontendUser.membership_level 对应，例如 0=VIP0，1=VIP1",
    )
    name = models.CharField(max_length=32, verbose_name="等级名称", db_comment="如 VIP0 / VIP1")
    join_fee_usdt = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name="加入费用 USDT",
        db_comment="升级/加入该等级所需 USDT；0 表示免费",
    )
    can_claim_free_tasks = models.BooleanField(default=True, verbose_name="可领取免费任务")
    can_claim_official_tasks = models.BooleanField(default=False, verbose_name="可领取 VIP 任务专区")
    can_claim_high_commission_tasks = models.BooleanField(default=False, verbose_name="可领取高佣金任务")
    daily_official_task_limit = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="每日 VIP 任务领取上限",
        db_comment="留空表示不限量；VIP1 可填 1，VIP2 可填 2，VIP3 可不限量",
    )
    unlimited_tasks = models.BooleanField(default=False, verbose_name="VIP任务不限量")
    withdraw_fee_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        verbose_name="提现手续费比例",
        db_comment="0.20 = 提现金额的 20 percent；0 = 不收手续费",
    )
    description = models.TextField(blank=True, default="", verbose_name="等级说明")
    sort_order = models.PositiveIntegerField(default=0, verbose_name="排序", db_comment="越小越靠前")
    is_active = models.BooleanField(default=True, verbose_name="启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "task_membership_level_config"
        verbose_name = "会员等级活动配置"
        verbose_name_plural = verbose_name
        ordering = ("sort_order", "level")

    def __str__(self):
        return f"{self.name}（Lv.{self.level}）"

    @classmethod
    def for_level(cls, level: int | None):
        try:
            n = int(level if level is not None else 0)
        except (TypeError, ValueError):
            n = 0
        return cls.objects.filter(level=n, is_active=True).first()


class TeamLeaderTier(models.Model):
    """团队长 / 超级代理扶持政策阶梯。"""

    PERIOD_CUMULATIVE = "cumulative"
    PERIOD_MONTHLY = "monthly"
    PERIOD_CHOICES = (
        (PERIOD_CUMULATIVE, "累计"),
        (PERIOD_MONTHLY, "每月"),
    )

    sort_order = models.PositiveIntegerField(default=0, verbose_name="排序", db_comment="越小越靠前")
    name = models.CharField(max_length=64, verbose_name="等级名称", db_comment="如 初级代理 / 中级合伙人")
    direct_vip_count = models.PositiveIntegerField(
        default=0,
        verbose_name="直推 VIP 人数门槛",
        db_comment="达到该直推 VIP 人数后满足人数门槛",
    )
    team_recharge_target_usdt = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name="团队总充值业绩目标 USDT",
    )
    target_period = models.CharField(
        max_length=16,
        choices=PERIOD_CHOICES,
        default=PERIOD_CUMULATIVE,
        verbose_name="业绩周期",
        db_comment="累计或每月",
    )
    team_performance_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        verbose_name="额外团队业绩提成比例",
        db_comment="0.02 = 2 percent",
    )
    description = models.TextField(blank=True, default="", verbose_name="说明")
    is_active = models.BooleanField(default=True, verbose_name="启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "task_team_leader_tier"
        verbose_name = "团队长扶持阶梯"
        verbose_name_plural = verbose_name
        ordering = ("sort_order", "id")

    def __str__(self):
        return self.name


class CheckInRecord(models.Model):
    """首页「每日签到」：按自然日（项目时区）一条记录。"""

    user = models.ForeignKey(
        FrontendUser,
        on_delete=models.CASCADE,
        related_name="check_ins",
        verbose_name="用户",
        db_comment="签到用户",
        db_constraint=False,
    )
    on_date = models.DateField(verbose_name="签到日期", db_comment="按 Asia/Shanghai 等当前激活时区的日历日")
    is_make_up = models.BooleanField(default=False, verbose_name="是否补签", db_comment="补签为 True")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="记录时间", db_comment="写入时间")

    class Meta:
        db_table = "task_check_in"
        verbose_name = "每日签到"
        verbose_name_plural = verbose_name
        unique_together = (("user", "on_date"),)
        ordering = ("-on_date", "-id")

    def __str__(self):
        return f"{self.user_id} {self.on_date}"


class DailyTaskDefinition(models.Model):
    """
    活动页「每日任务」：按自然日（项目时区）统计进度，零点换日；
    达标后用户主动领取 TH/USDT（每档每人每天最多领一次）。
    """

    METRIC_PLATFORM_TASKS_DONE_TODAY = "platform_tasks_done_today"
    METRIC_CHOICES = (
        (
            METRIC_PLATFORM_TASKS_DONE_TODAY,
            "当日完成任务数（已录用且已完结：已发奖或无展示奖励且已录用）",
        ),
    )

    sort_order = models.PositiveIntegerField(default=0, verbose_name="排序", db_comment="列表顺序，越小越靠前")
    title = models.CharField(max_length=128, verbose_name="任务标题", db_comment="如「完成 3 个任务」")
    metric_code = models.CharField(
        max_length=64,
        choices=METRIC_CHOICES,
        default=METRIC_PLATFORM_TASKS_DONE_TODAY,
        verbose_name="统计口径",
        db_comment="决定 progress_current 如何计算",
    )
    target_count = models.PositiveIntegerField(
        default=1,
        verbose_name="目标数量",
        db_comment="当日进度达到该值可领取",
    )
    reward_usdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0"),
        verbose_name="奖励 USDT",
        db_comment="领取时入账；可为 0",
    )
    reward_th = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0"),
        verbose_name="奖励 TH Coin",
        db_comment="领取时入账 frozen；可为 0",
    )
    is_active = models.BooleanField(default=True, verbose_name="启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "task_daily_task_definition"
        verbose_name = "每日任务配置"
        verbose_name_plural = verbose_name
        ordering = ("sort_order", "id")

    def __str__(self):
        return f"{self.title} ({self.get_metric_code_display()}) ≥{self.target_count}"


class DailyTaskDayClaim(models.Model):
    """某用户在某自然日已领取某一档每日任务奖励。"""

    user = models.ForeignKey(
        FrontendUser,
        on_delete=models.CASCADE,
        related_name="daily_task_day_claims",
        verbose_name="用户",
        db_constraint=False,
    )
    definition = models.ForeignKey(
        DailyTaskDefinition,
        on_delete=models.CASCADE,
        related_name="day_claims",
        verbose_name="每日任务",
    )
    on_date = models.DateField(verbose_name="归属日期", db_comment="按当前激活时区的日历日")
    claimed_at = models.DateTimeField(auto_now_add=True, verbose_name="领取时间")

    class Meta:
        db_table = "task_daily_task_day_claim"
        verbose_name = "每日任务领取记录"
        verbose_name_plural = verbose_name
        unique_together = (("user", "definition", "on_date"),)
        ordering = ("-on_date", "-id")

    def __str__(self):
        return f"user={self.user_id} def={self.definition_id} {self.on_date}"


class InviteAchievementTier(models.Model):
    """
    活动页「邀请成就」阶梯：后台配置人数阈值与 USDT/TH 奖励；
    直邀且下级 status=true 的人数达到阈值后可领取（每档每人限领一次）。
    """

    sort_order = models.PositiveIntegerField(default=0, verbose_name="排序", db_comment="列表展示顺序，越小越靠前")
    invite_threshold = models.PositiveIntegerField(verbose_name="需邀请人数", db_comment="有效直邀人数达到该值可领")
    title = models.CharField(max_length=64, default="推荐专家", verbose_name="成就标题")
    reward_usdt = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0"),
        verbose_name="奖励 USDT",
        db_comment="领取时入账钱包 USDT；可为 0",
    )
    reward_th = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal("0"),
        verbose_name="奖励 TH Coin",
        db_comment="领取时入账 TH；可为 0",
    )
    is_active = models.BooleanField(default=True, verbose_name="启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "task_invite_achievement_tier"
        verbose_name = "邀请成就阶梯"
        verbose_name_plural = verbose_name
        ordering = ("sort_order", "invite_threshold", "id")

    def __str__(self):
        return f"{self.title} ≥{self.invite_threshold}人"


class InviteAchievementClaim(models.Model):
    user = models.ForeignKey(
        FrontendUser,
        on_delete=models.CASCADE,
        related_name="invite_achievement_claims",
        verbose_name="用户",
        db_constraint=False,
    )
    tier = models.ForeignKey(
        InviteAchievementTier,
        on_delete=models.CASCADE,
        related_name="claims",
        verbose_name="阶梯",
    )
    claimed_at = models.DateTimeField(auto_now_add=True, verbose_name="领取时间")

    class Meta:
        db_table = "task_invite_achievement_claim"
        verbose_name = "邀请成就领取记录"
        verbose_name_plural = verbose_name
        unique_together = (("user", "tier"),)
        ordering = ("-claimed_at",)

    def __str__(self):
        return f"user={self.user_id} tier={self.tier_id}"


class TelegramStartInvitePending(models.Model):
    """
    用户通过 https://t.me/<Bot>?start=<payload> 打开 Bot 并点「启动」后，
    Telegram 会把 payload 随 /start 推给 Webhook；在用户随后 POST Mini App 登录前暂存于此，
    以便 initData 无 start_param 时仍能绑定 referrer。
    """

    telegram_id = models.BigIntegerField(unique=True, db_index=True, verbose_name="Telegram 用户 ID")
    start_payload = models.CharField(max_length=64, verbose_name="start 参数", db_comment="与 deep link ?start= 一致，最长 64")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "taskhub_telegram_start_invite_pending"
        verbose_name = "Telegram /start 待绑定邀请"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"tg:{self.telegram_id} {self.start_payload!r}"


class IntegrationSecretConfig(models.Model):
    """
    全局第三方 API 密钥（后台仅维护一条）。
    字段有值时优先生效；留空则仍使用环境变量或 core/*_secrets.py（见 core/settings.py）。
    """

    telegram_bot_token = models.TextField(
        blank=True,
        default="",
        verbose_name="Telegram Bot Token",
        db_comment="Mini App 登录与入群校验 getChatMember；与 BotFather 一致",
    )
    twitter_bearer_token = models.TextField(
        blank=True,
        default="",
        verbose_name="Twitter / X Bearer Token",
        db_comment="API v2 只读 Bearer，用于转发/关注校验",
    )
    apify_api_token = models.TextField(
        blank=True,
        default="",
        verbose_name="Apify API Token",
        db_comment="Instagram / TikTok 等 Actor 调用",
    )
    apify_instagram_actor_id = models.CharField(
        max_length=256,
        blank=True,
        default="",
        verbose_name="Apify Instagram Actor ID",
        db_comment="默认 apify/instagram-profile-scraper；留空用 settings",
    )
    apify_instagram_timeout_sec = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Instagram 请求超时（秒）",
        db_comment="留空则使用 settings / 环境变量",
    )
    apify_twitter_follow_actor_id = models.CharField(
        max_length=256,
        blank=True,
        default="",
        verbose_name="Apify Twitter 关注 Actor ID",
        db_comment="默认 automation-lab/twitter-scraper（following 模式）",
    )
    apify_twitter_repost_actor_id = models.CharField(
        max_length=256,
        blank=True,
        default="",
        verbose_name="Apify Twitter 转发 Actor ID",
        db_comment="默认 api-ninja/x-twitter-replies-retweets-scraper",
    )
    apify_twitter_timeout_sec = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Twitter 请求超时（秒）",
        db_comment="留空则使用 settings / 环境变量",
    )
    apify_twitter_following_max_results = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Twitter 关注校验最大抓取数",
        db_comment="用于从 following 列表中查找目标账号，留空则使用 settings / 环境变量",
    )
    apify_twitter_auth_token = models.TextField(
        blank=True,
        default="",
        verbose_name="Twitter auth_token Cookie",
        db_comment="用于 Apify 关注校验；留空则使用 Actor 默认或 settings / 环境变量",
    )
    apify_twitter_ct0 = models.TextField(
        blank=True,
        default="",
        verbose_name="Twitter ct0 Cookie",
        db_comment="用于 Apify 关注校验；留空则使用 Actor 默认或 settings / 环境变量",
    )
    apify_tiktok_actor_id = models.CharField(
        max_length=256,
        blank=True,
        default="",
        verbose_name="Apify TikTok Actor ID",
        db_comment="默认 clockworks/tiktok-scraper",
    )
    apify_tiktok_timeout_sec = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="TikTok 请求超时（秒）",
        db_comment="留空则使用 settings / 环境变量",
    )
    apify_tiktok_results_per_page = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="TikTok Reposts 每页条数",
        db_comment="留空则使用 settings / 环境变量",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "task_integration_secret_config"
        verbose_name = "第三方集成密钥"
        verbose_name_plural = verbose_name

    def __str__(self):
        return "第三方集成密钥（全局）"

    @classmethod
    def get(cls):
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create()
        return obj


class ApiToken(models.Model):
    user = models.OneToOneField(
        FrontendUser,
        on_delete=models.CASCADE,
        related_name="api_token",
        verbose_name="所属用户",
        db_comment="Token 对应的用户",
    )
    key = models.CharField(max_length=64, unique=True, verbose_name="Token", db_comment="鉴权令牌")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="签发时间", db_comment="Token 创建时间")
    last_used_at = models.DateTimeField(blank=True, null=True, verbose_name="最近使用时间", db_comment="最近一次鉴权请求时间")

    class Meta:
        db_table = "task_api_token"
        verbose_name = "API Token"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.user.username} token"

    @classmethod
    def issue_for_user(cls, user):
        token_value = secrets.token_hex(32)
        token, _ = cls.objects.update_or_create(user=user, defaults={"key": token_value})
        token.last_used_at = timezone.now()
        token.save(update_fields=["last_used_at"])
        return token


class OnlineFeedback(models.Model):
    STATUS_PENDING = "pending"
    STATUS_REPLIED = "replied"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = (
        (STATUS_PENDING, "待回复"),
        (STATUS_REPLIED, "已回复"),
        (STATUS_CLOSED, "已关闭"),
    )

    user = models.ForeignKey(
        FrontendUser,
        on_delete=models.CASCADE,
        related_name="online_feedbacks",
        verbose_name="反馈用户",
        db_comment="提交反馈的前台用户",
    )
    title = models.CharField(max_length=120, verbose_name="反馈标题", db_comment="用户填写的反馈标题")
    content = models.TextField(verbose_name="反馈内容", db_comment="用户填写的反馈详细内容")
    contact = models.CharField(
        max_length=120,
        blank=True,
        default="",
        verbose_name="联系方式",
        db_comment="用户可选填写的联系方式",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        verbose_name="处理状态",
        db_comment="反馈处理状态",
    )
    admin_reply = models.TextField(blank=True, default="", verbose_name="后台回复", db_comment="后台给用户的回复内容")
    replied_by = models.CharField(
        max_length=120,
        blank=True,
        default="",
        verbose_name="回复人",
        db_comment="后台回复人的显示名",
    )
    replied_at = models.DateTimeField(blank=True, null=True, verbose_name="回复时间", db_comment="后台回复时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="提交时间", db_comment="反馈提交时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间", db_comment="最后更新时间")

    class Meta:
        db_table = "task_online_feedback"
        verbose_name = "在线反馈"
        verbose_name_plural = verbose_name
        ordering = ("-updated_at", "-id")

    def __str__(self):
        return f"{self.user}：{self.title}"

    def save(self, *args, **kwargs):
        if self.admin_reply.strip() and not self.replied_at:
            self.replied_at = timezone.now()
        if self.admin_reply.strip() and self.status == self.STATUS_PENDING:
            self.status = self.STATUS_REPLIED
        super().save(*args, **kwargs)
