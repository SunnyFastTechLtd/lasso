from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0003_chatgeneratedfile'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='knowledge_base_ids',
            field=models.JSONField(blank=True, default=list, verbose_name='关联知识库ID'),
        ),
    ]
