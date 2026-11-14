from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('skills', '0006_backfill_skill_versions'),
    ]

    operations = [
        migrations.AddField(
            model_name='skill',
            name='rollout_percent',
            field=models.PositiveSmallIntegerField(
                default=100, help_text='基础范围内按用户稳定分桶放开的百分比：0=不放开，100=全量',
                validators=[MinValueValidator(0), MaxValueValidator(100)],
                verbose_name='灰度比例'),
        ),
        migrations.AddField(
            model_name='skill',
            name='applicable_users',
            field=models.ManyToManyField(
                blank=True, related_name='+', to=settings.AUTH_USER_MODEL,
                verbose_name='定向用户'),
        ),
    ]
