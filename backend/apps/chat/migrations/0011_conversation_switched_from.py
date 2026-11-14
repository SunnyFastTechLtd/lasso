from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0010_conversation_use_preferences_userpreference'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='switched_from',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='switch_successors',
                to='chat.conversation',
                verbose_name='切换来源会话',
            ),
        ),
    ]
