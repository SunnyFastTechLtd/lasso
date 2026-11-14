from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('agenthub', '0001_initial'),
        ('chat', '0005_alter_conversation_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='agent',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='conversations', to='agenthub.agentprofile',
                verbose_name='所属智能体',
            ),
        ),
    ]
