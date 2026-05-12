"""
对外 API 路由。新增 path 时请在 `taskhub/api_endpoints.py` 的 `PUBLIC_ENDPOINTS` 中登记，
并视需要更新 `docs/taskhub_api.md` 正文说明；可选执行 `python manage.py sync_taskhub_api_docs`。
"""

from django.urls import path

from . import activities_api
from . import daily_tasks_api
from . import api_views
from . import miniapp_api
from . import profile_center_api
from . import ranking_api
from . import telegram_webhook


urlpatterns = [
    path("health/", api_views.health_api, name="taskhub-health"),
    path("docs/", api_views.docs_api, name="taskhub-docs"),
    path("auth/register/", api_views.register_api, name="taskhub-register"),
    path("auth/login/", api_views.login_api, name="taskhub-login"),
    path("auth/telegram/", miniapp_api.telegram_auth_api, name="taskhub-auth-telegram"),
    path(
        "telegram/miniapp-login/",
        miniapp_api.telegram_auth_api,
        name="taskhub-auth-telegram-miniapp-alias",
    ),
    path(
        "telegram/webhook/",
        telegram_webhook.telegram_bot_webhook_api,
        name="taskhub-telegram-bot-webhook",
    ),
    path("auth/logout/", api_views.logout_api, name="taskhub-logout"),
    path("me/ping/", api_views.me_ping_api, name="taskhub-me-ping"),
    path("me/home/", miniapp_api.my_home_api, name="taskhub-me-home"),
    path("me/center/", profile_center_api.me_center_api, name="taskhub-me-center"),
    path("me/rewards/ledger/", profile_center_api.me_rewards_ledger_api, name="taskhub-me-rewards-ledger"),
    path("me/recharges/", profile_center_api.me_recharges_api, name="taskhub-me-recharges"),
    path("me/withdrawals/", profile_center_api.me_withdrawals_api, name="taskhub-me-withdrawals"),
    path("me/feedback/", profile_center_api.me_feedback_api, name="taskhub-me-feedback"),
    path(
        "me/membership/purchase/",
        profile_center_api.me_membership_purchase_api,
        name="taskhub-me-membership-purchase",
    ),
    path("me/bindings/accounts/", profile_center_api.me_bound_accounts_api, name="taskhub-me-bindings-accounts"),
    path(
        "me/settings/notifications/",
        profile_center_api.me_notification_settings_api,
        name="taskhub-me-settings-notifications",
    ),
    path("me/check-in/", miniapp_api.my_check_in_api, name="taskhub-me-check-in"),
    path("me/check-in/make-up/", miniapp_api.my_check_in_makeup_api, name="taskhub-me-check-in-makeup"),
    path("invite/activity-rules/", activities_api.invite_activity_rules_api, name="taskhub-invite-activity-rules"),
    path("me/invite-achievements/", activities_api.me_invite_achievements_api, name="taskhub-me-invite-achievements"),
    path(
        "me/invite-achievements/claim/",
        activities_api.me_invite_achievements_claim_api,
        name="taskhub-me-invite-achievements-claim",
    ),
    path("daily-tasks/", daily_tasks_api.daily_tasks_list_api, name="taskhub-daily-tasks"),
    path("daily-tasks/claim/", daily_tasks_api.daily_tasks_claim_api, name="taskhub-daily-tasks-claim"),
    path("tasks/mandatory/", miniapp_api.mandatory_tasks_api, name="taskhub-mandatory-tasks"),
    path("tasks/center/", api_views.tasks_center_api, name="taskhub-tasks-center"),
    path("rankings/platform-stats/", ranking_api.rankings_platform_stats_api, name="taskhub-rankings-platform-stats"),
    path(
        "rankings/commission-leaderboard/",
        ranking_api.rankings_commission_leaderboard_api,
        name="taskhub-rankings-commission-leaderboard",
    ),
    path(
        "rankings/task-leaderboard/",
        ranking_api.rankings_commission_leaderboard_api,
        name="taskhub-rankings-task-leaderboard-alias",
    ),
    path(
        "rankings/invite-leaderboard/",
        ranking_api.rankings_invite_leaderboard_api,
        name="taskhub-rankings-invite-leaderboard",
    ),
    path(
        "me/ranking/invite-overview/",
        ranking_api.me_ranking_invite_overview_api,
        name="taskhub-me-ranking-invite-overview",
    ),
    path("me/ranking/invitees/", ranking_api.me_ranking_invitees_api, name="taskhub-me-ranking-invitees"),
    path("me/ranking/context/", ranking_api.me_ranking_context_api, name="taskhub-me-ranking-context"),
    # 兼容前端误写为复数 me/rankings/（与全站 rankings/ 对称）
    path(
        "me/rankings/invite-overview/",
        ranking_api.me_ranking_invite_overview_api,
        name="taskhub-me-rankings-invite-overview-alias",
    ),
    path("me/rankings/invitees/", ranking_api.me_ranking_invitees_api, name="taskhub-me-rankings-invitees-alias"),
    path("me/rankings/context/", ranking_api.me_ranking_context_api, name="taskhub-me-rankings-context-alias"),
    path("me/profile/", api_views.my_profile_api, name="taskhub-profile"),
    path("me/published-tasks/", api_views.my_published_tasks_api, name="taskhub-my-published-tasks"),
    path("me/applied-tasks/", api_views.my_applied_tasks_api, name="taskhub-my-applied-tasks"),
    path("me/task-records/", api_views.my_task_records_api, name="taskhub-my-task-records"),
    path(
        "me/applications/<int:application_id>/verify-twitter/",
        api_views.application_twitter_verify_api,
        name="taskhub-application-verify-twitter",
    ),
    path(
        "me/applications/<int:application_id>/verify-youtube/",
        api_views.application_youtube_verify_api,
        name="taskhub-application-verify-youtube",
    ),
    path(
        "me/applications/<int:application_id>/verify-instagram/",
        api_views.application_instagram_verify_api,
        name="taskhub-application-verify-instagram",
    ),
    path(
        "me/applications/<int:application_id>/verify-tiktok/",
        api_views.application_tiktok_verify_api,
        name="taskhub-application-verify-tiktok",
    ),
    path(
        "me/applications/<int:application_id>/verify-social-action/",
        api_views.application_social_action_verify_api,
        name="taskhub-application-verify-social-action",
    ),
    path(
        "me/applications/<int:application_id>/verify-telegram-group/",
        api_views.application_telegram_group_verify_api,
        name="taskhub-application-verify-telegram-group",
    ),
    path(
        "me/applications/<int:application_id>/upload-proof/",
        api_views.application_upload_proof_api,
        name="taskhub-application-upload-proof",
    ),
    path("categories/", api_views.category_list_api, name="taskhub-categories"),
    path("tasks/", api_views.task_collection_api, name="taskhub-task-collection"),
    path("tasks/<int:task_id>/", api_views.task_detail_api, name="taskhub-task-detail"),
    path("tasks/<int:task_id>/apply/", api_views.task_apply_api, name="taskhub-task-apply"),
    path("tasks/<int:task_id>/applications/", api_views.task_applications_api, name="taskhub-task-applications"),
    path("applications/<int:application_id>/", api_views.application_review_api, name="taskhub-application-review"),
    path(
        "integrations/mobidea/postback/",
        api_views.mobidea_postback_api,
        name="taskhub-mobidea-postback",
    ),
]
