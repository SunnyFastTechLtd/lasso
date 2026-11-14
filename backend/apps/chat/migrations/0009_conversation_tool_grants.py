from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0008_conversation_agent_version'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='tool_grants',
            field=models.JSONField(blank=True, default=list, verbose_name='会话工具授权'),
        ),
    ]
