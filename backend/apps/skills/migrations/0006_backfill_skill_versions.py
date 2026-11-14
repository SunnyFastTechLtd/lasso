"""C4：为存量已审核技能回填 v1 版本快照。

幂等：已有版本记录的技能直接跳过；反向迁移不删除任何历史，避免误删。
P0 不产生 SkillFile 行，附件回填为兜底逻辑：镜像可读则回填，不可读则跳过。
"""
import hashlib
from pathlib import Path

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    Skill = apps.get_model('skills', 'Skill')
    SkillVersion = apps.get_model('skills', 'SkillVersion')
    SkillVersionFile = apps.get_model('skills', 'SkillVersionFile')
    SkillBlob = apps.get_model('skills', 'SkillBlob')
    SkillFile = apps.get_model('skills', 'SkillFile')

    mirror_root = Path(settings.SKILLS_MIRROR_DIR)

    for skill in Skill.objects.exclude(approved_body='').iterator():
        existing = SkillVersion.objects.filter(skill_id=skill.pk).order_by('-version').first()
        if existing is not None:
            Skill.objects.filter(pk=skill.pk).update(current_version=existing.version)
            continue

        body = skill.approved_body or ''
        digest = skill.body_hash or hashlib.sha256(body.encode('utf-8')).hexdigest()
        approved_at = skill.reviewed_at or skill.updated_at or timezone.now()
        version = SkillVersion.objects.create(
            skill_id=skill.pk,
            version=1,
            body=body,
            body_hash=digest,
            char_count=skill.char_count or len(body),
            change_summary='升级前存量版本',
            approved_by_id=skill.reviewed_by_id,
            approved_at=approved_at,
        )

        for item in SkillFile.objects.filter(skill_id=skill.pk):
            if item.entry or item.rel_path == skill.entry_file:
                continue
            raw = None
            try:
                path = mirror_root / skill.slug / item.rel_path
                if path.is_file():
                    raw = path.read_bytes()
            except OSError:
                raw = None
            if raw is None:
                continue
            sha = hashlib.sha256(raw).hexdigest()
            blob, _ = SkillBlob.objects.get_or_create(
                sha256=sha, defaults={'size': len(raw), 'content': raw})
            SkillVersionFile.objects.get_or_create(
                version_id=version.pk,
                rel_path=item.rel_path,
                defaults={'blob_id': blob.pk, 'size': blob.size, 'sha256': blob.sha256},
            )

        Skill.objects.filter(pk=skill.pk).update(current_version=1)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('skills', '0005_skill_package_versions'),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_code=noop),
    ]
