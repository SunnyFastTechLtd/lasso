from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0006_alter_auditlog_action'),
    ]

    operations = [
        migrations.AddField(
            model_name='auditlog',
            name='egress',
            field=models.BooleanField(db_index=True, default=False, verbose_name='走外网出口'),
        ),
    ]
