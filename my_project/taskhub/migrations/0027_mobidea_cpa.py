# Generated manually for Mobidea CPA postback integration.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("taskhub", "0026_online_feedback"),
    ]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="interaction_type",
            field=models.CharField(
                choices=[
                    ("none", "无（不按下面规则校验）"),
                    ("account_binding", "账号绑定（首页「绑定 Twitter/TikTok…」卡片）"),
                    ("join_community", "加入社群（如 Telegram 入群/频道）"),
                    ("follow", "关注"),
                    ("repost", "转发 / Repost / Retweet"),
                    ("like", "点赞"),
                    ("comment", "评论"),
                    ("watch_video", "观看视频"),
                    ("external_vote", "外部网页投票"),
                    ("screenshot_proof", "上传截图审核"),
                    ("cpa_offer", "CPA 外部任务（Postback 自动确认）"),
                ],
                db_comment="账号绑定/关注/评论/看视频/外站投票等",
                default="none",
                max_length=32,
                verbose_name="必做任务类型",
            ),
        ),
        migrations.AlterField(
            model_name="task",
            name="verification_mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("user_self_confirm", "用户自行确认完成"),
                    ("profile_link_proof", "简介/频道留指定链接证明"),
                    ("screenshot_review", "上传截图待审核"),
                    ("postback", "第三方 Postback 自动确认"),
                ],
                db_comment="由业务推导，也可在后台手动改",
                max_length=32,
                null=True,
                verbose_name="校验方式",
            ),
        ),
        migrations.AddField(
            model_name="taskapplication",
            name="external_click_id",
            field=models.CharField(
                blank=True,
                db_comment="传给第三方的唯一 click_id/pub_click_id，用于回调匹配报名",
                max_length=128,
                null=True,
                unique=True,
                verbose_name="外部点击 ID",
            ),
        ),
        migrations.AddField(
            model_name="taskapplication",
            name="external_provider",
            field=models.CharField(
                blank=True,
                db_comment="如 mobidea；CPA/Postback 任务使用",
                db_index=True,
                max_length=32,
                null=True,
                verbose_name="外部任务来源",
            ),
        ),
        migrations.AddField(
            model_name="taskapplication",
            name="external_started_at",
            field=models.DateTimeField(
                blank=True,
                db_comment="用户领取后首次生成第三方跳转链接的时间",
                null=True,
                verbose_name="外部任务打开时间",
            ),
        ),
        migrations.CreateModel(
            name="MobideaConversion",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                (
                    "click_id",
                    models.CharField(
                        db_comment="Mobidea 回传的 {{EXTERNAL_ID}} / pub_click_id",
                        max_length=128,
                        unique=True,
                        verbose_name="Click ID",
                    ),
                ),
                (
                    "offer_id",
                    models.CharField(
                        blank=True,
                        db_comment="Mobidea offer_id",
                        max_length=64,
                        null=True,
                        verbose_name="Mobidea Offer ID",
                    ),
                ),
                (
                    "payout",
                    models.DecimalField(
                        blank=True,
                        db_comment="Mobidea 回传的 {{MONEY}}",
                        decimal_places=4,
                        max_digits=12,
                        null=True,
                        verbose_name="Mobidea 收益",
                    ),
                ),
                (
                    "currency",
                    models.CharField(default="CNY", db_comment="Mobidea payout 币种", max_length=12, verbose_name="币种"),
                ),
                (
                    "raw_payload",
                    models.JSONField(blank=True, db_comment="Mobidea postback 参数", default=dict, verbose_name="原始回调"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("received", "已接收"),
                            ("processed", "已处理并发奖"),
                            ("duplicate", "重复回调"),
                            ("rejected", "已拒绝"),
                            ("failed", "处理失败"),
                        ],
                        db_comment="Mobidea 回调处理状态",
                        default="received",
                        max_length=20,
                        verbose_name="处理状态",
                    ),
                ),
                (
                    "message",
                    models.CharField(
                        blank=True,
                        db_comment="错误或重复说明",
                        max_length=255,
                        null=True,
                        verbose_name="处理说明",
                    ),
                ),
                (
                    "received_at",
                    models.DateTimeField(auto_now_add=True, db_comment="回调接收时间", verbose_name="接收时间"),
                ),
                (
                    "processed_at",
                    models.DateTimeField(blank=True, db_comment="处理完成时间", null=True, verbose_name="处理时间"),
                ),
                (
                    "task_application",
                    models.ForeignKey(
                        blank=True,
                        db_comment="通过 click_id 匹配到的 TaskApplication",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="mobidea_conversions",
                        to="taskhub.taskapplication",
                        verbose_name="关联报名",
                    ),
                ),
            ],
            options={
                "verbose_name": "Mobidea 转化回调",
                "verbose_name_plural": "Mobidea 转化回调",
                "db_table": "taskhub_mobidea_conversion",
                "ordering": ("-received_at",),
            },
        ),
    ]
