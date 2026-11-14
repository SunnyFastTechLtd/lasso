from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0012_pending_permission'),
    ]

    operations = [
        migrations.AddField(
            model_name='message',
            name='input_tokens',
            field=models.PositiveIntegerField(default=0, verbose_name='输入 token 数'),
        ),
        migrations.AddField(
            model_name='message',
            name='output_tokens',
            field=models.PositiveIntegerField(default=0, verbose_name='输出 token 数'),
        ),
    ]
