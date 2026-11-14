from django.db import migrations, models


def map_is_public_to_scope(apps, schema_editor):
    """存量数据：is_public=True → public，False → private"""
    KnowledgeBase = apps.get_model('knowledge', 'KnowledgeBase')
    KnowledgeBase.objects.filter(is_public=True).update(scope='public')
    KnowledgeBase.objects.filter(is_public=False).update(scope='private')


class Migration(migrations.Migration):

    dependencies = [
        ('knowledge', '0004_documentchunk_is_active'),
    ]

    operations = [
        migrations.AddField(
            model_name='knowledgebase',
            name='scope',
            field=models.CharField(
                choices=[('public', '公开'), ('department', '本部门'), ('private', '私有')],
                default='public',
                max_length=20,
                verbose_name='可见范围',
            ),
        ),
        migrations.AddField(
            model_name='knowledgebase',
            name='department',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='所属部门'),
        ),
        migrations.RunPython(map_is_public_to_scope, migrations.RunPython.noop),
    ]
