from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('skills', '0002_skill_approved_body'),
    ]

    operations = [
        migrations.AddField(
            model_name='skill',
            name='scan_warnings',
            field=models.JSONField(blank=True, default=list, verbose_name='风险扫描提示'),
        ),
    ]
