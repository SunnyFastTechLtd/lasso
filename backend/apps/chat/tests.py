import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from django.core.files.base import ContentFile

from backend.apps.users.models import AuditLog, User
from backend.apps.chat.models import (
    Conversation, Message, ChatGeneratedFile, ToolCallBody, UserPreference,
    PendingPermission, TOOL_BODY_MAX_CHARS,
)
from backend.apps.chat.preferences import active_preference_texts
from backend.apps.chat.views import ChatViewSet
from backend.apps.chat.permissions import PermissionSession, resolve_permission
from backend.apps.chat.services import needs_rewrite
from backend.apps.settings.models import SystemSetting
from backend.apps.agenthub.models import AgentProfile, AgentStatus
from backend.apps.common.choices import Visibility
from backend.apps.agents.tools import generate_file


def _make_user(username):
    return User.objects.create_user(username=username, password='demo-pass-9x2')


def _make_txt_file(name: str, content: str, size_mb: float | None = None):
    """构造一个可上传的内存文本文件；size_mb 用于生成大文件"""
    if size_mb is not None:
        content = 'x' * int(size_mb * 1024 * 1024)
    f = io.BytesIO(content.encode('utf-8'))
    f.name = name
    return f


class ConversationCRUDTests(TestCase):
    """会话管理：多用户隔离 + 类型过滤 + 消息列表 + 删除"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.bob = _make_user('bob')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.conv = Conversation.objects.create(user=self.alice, title='测试会话', type='chat')
        Conversation.objects.create(user=self.alice, title='知识库会话', type='knowledge')
        Conversation.objects.create(user=self.bob, title='别人的会话', type='chat')

    def test_list_only_own_conversations(self):
        resp = self.client.get('/api/chat/conversations/')
        self.assertEqual(resp.status_code, 200, resp.content)
        items = resp.data.get('results', resp.data)
        titles = [c['title'] for c in items]
        self.assertIn('测试会话', titles)
        self.assertNotIn('别人的会话', titles)

    def test_filter_by_type(self):
        resp = self.client.get('/api/chat/conversations/?type=knowledge')
        items = resp.data.get('results', resp.data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['type'], 'knowledge')

    def test_create_conversation_binds_owner(self):
        resp = self.client.post('/api/chat/conversations/', {'title': '新会话'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Conversation.objects.get(id=resp.data['id']).user, self.alice)

    def test_messages_endpoint(self):
        Message.objects.create(conversation=self.conv, role='user', content='你好')
        Message.objects.create(conversation=self.conv, role='assistant', content='你好呀')
        resp = self.client.get(f'/api/chat/conversations/{self.conv.id}/messages/')
        self.assertEqual(resp.status_code, 200, resp.content)
        items = resp.data.get('results', resp.data)
        self.assertEqual([m['role'] for m in items], ['user', 'assistant'])

    def test_cannot_access_others_conversation(self):
        other = Conversation.objects.get(user=self.bob)
        resp = self.client.get(f'/api/chat/conversations/{other.id}/messages/')
        self.assertEqual(resp.status_code, 404)

    def test_delete_conversation(self):
        resp = self.client.post(f'/api/chat/conversations/{self.conv.id}/delete_conversation/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(Conversation.objects.filter(id=self.conv.id).exists())


class ChatSendTests(TestCase):
    """非流式对话：mock 智能体，验证消息落库与应答"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    @patch('backend.apps.chat.views.run_agent', return_value='这是回答')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    def test_send_creates_conversation_and_messages(self, _create, _run):
        resp = self.client.post('/api/chat/chat/send/', {'message': '你好'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['answer'], '这是回答')

        conv = Conversation.objects.get(id=resp.data['conversation_id'])
        self.assertEqual(conv.user, self.alice)
        self.assertEqual(conv.title, '你好')
        self.assertEqual(conv.message_count, 2)
        roles = list(conv.messages.order_by('created_at').values_list('role', flat=True))
        self.assertEqual(roles, ['user', 'assistant'])

    def test_send_requires_message(self):
        resp = self.client.post('/api/chat/chat/send/', {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_send_with_others_conversation_id_returns_404(self):
        bob = _make_user('bob')
        other = Conversation.objects.create(user=bob, title='私有')
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '你好', 'conversation_id': other.id,
        }, format='json')
        self.assertEqual(resp.status_code, 404)

    @patch('backend.apps.chat.views.run_agent', side_effect=RuntimeError('模型挂了'))
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    def test_agent_failure_returns_503(self, _create, _run):
        resp = self.client.post('/api/chat/chat/send/', {'message': '你好'}, format='json')
        self.assertEqual(resp.status_code, 503)
        # 失败不落消息
        self.assertEqual(Message.objects.count(), 0)

    @patch('backend.apps.chat.views.run_agent', return_value='已生成文件《report.docx》[GENERATED_FILE:{gid}]，请下载。')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    def test_send_links_generated_files(self, _create, mock_run):
        f = ChatGeneratedFile.objects.create(
            owner=self.alice,
            file_name='report.docx',
            file_type='docx',
            file_size=8,
            file=ContentFile(b'fake', name='report.docx'),
        )
        # 用真实 id 替换占位符
        mock_run.return_value = f'已生成文件《report.docx》[GENERATED_FILE:{f.id}]，请下载。'
        resp = self.client.post('/api/chat/chat/send/', {'message': '生成报告'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data['generated_files']), 1)
        self.assertEqual(resp.data['generated_files'][0]['id'], f.id)

        f.refresh_from_db()
        self.assertIsNotNone(f.message)
        self.assertIn('generated_files', f.message.meta)


class ChatAttachmentUploadTests(TestCase):
    """对话临时附件上传：解析、截断、大小限制、认证"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.url = '/api/chat/chat/upload_attachment/'

    def test_upload_txt_extracts_text(self):
        f = _make_txt_file('report.txt', '本年度营收增长 30%。')
        resp = self.client.post(self.url, {'file': f}, format='multipart')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['name'], 'report.txt')
        self.assertEqual(resp.data['type'], 'txt')
        self.assertIn('营收增长', resp.data['text'])
        self.assertFalse(resp.data['truncated'])

    def test_upload_truncate_long_text(self):
        long_text = '增' * 7000
        f = _make_txt_file('long.txt', long_text)
        resp = self.client.post(self.url, {'file': f}, format='multipart')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data['text']), 6000)
        self.assertTrue(resp.data['truncated'])

    def test_upload_rejects_oversize_file(self):
        f = _make_txt_file('big.txt', '', size_mb=11)
        resp = self.client.post(self.url, {'file': f}, format='multipart')
        self.assertEqual(resp.status_code, 413)

    def test_upload_rejects_unsupported_type(self):
        f = _make_txt_file('image.png', 'not an image')
        resp = self.client.post(self.url, {'file': f}, format='multipart')
        self.assertEqual(resp.status_code, 415)

    def test_upload_requires_authentication(self):
        self.client.force_authenticate(user=None)
        f = _make_txt_file('report.txt', 'hello')
        resp = self.client.post(self.url, {'file': f}, format='multipart')
        self.assertEqual(resp.status_code, 401)


class ChatAttachmentSendTests(TestCase):
    """发送消息时附件随消息落库并注入 prompt"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    @patch('backend.apps.chat.views.run_agent', return_value='根据附件，营收增长 30%。')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    def test_send_with_attachment_persists_and_injects(self, _create, _run):
        attachments = [{'name': 'report.txt', 'text': '本年度营收增长 30%。'}]
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '增长多少？',
            'attachments': attachments,
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

        conv = Conversation.objects.get(id=resp.data['conversation_id'])
        user_msg = conv.messages.get(role='user')
        self.assertEqual(user_msg.attachments, attachments)
        # 原始消息落库，不带注入前缀
        self.assertEqual(user_msg.content, '增长多少？')
        # 注入后的 prompt 传给 agent
        prompt = _run.call_args[0][2]
        self.assertIn('【附件：report.txt】', prompt)
        self.assertIn('本年度营收增长 30%。', prompt)
        self.assertIn('用户问题：增长多少？', prompt)

    @patch('backend.apps.chat.views.run_agent', return_value='已根据附件作答。')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    def test_upload_response_sent_back_verbatim(self, _create, _run):
        """D5：前端把上传接口的响应原样带回（size 整数、truncated 布尔），不能被校验拒掉"""
        upload = self.client.post(
            '/api/chat/chat/upload_attachment/',
            {'file': _make_txt_file('long.txt', '增' * 7000)},
            format='multipart',
        )
        self.assertEqual(upload.status_code, 200, upload.content)
        self.assertIs(upload.data['truncated'], True)

        resp = self.client.post(
            '/api/chat/chat/send/',
            {'message': '总结这份材料', 'attachments': [upload.data]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        conv = Conversation.objects.get(id=resp.data['conversation_id'])
        user_msg = conv.messages.get(role='user')
        stored = user_msg.attachments[0]
        self.assertEqual(stored['name'], 'long.txt')
        # 元数据保持原类型，回显大小时不必再猜字符串还是数字
        self.assertIsInstance(stored['size'], int)
        self.assertIs(stored['truncated'], True)
        prompt = _run.call_args[0][2]
        self.assertIn('【附件：long.txt】', prompt)


class ChatAssistantLinkTests(TestCase):
    """对话 → 助理联动：user_id 透传、非日程意图不受影响"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    @patch('backend.apps.chat.views.run_agent', return_value='已为你创建日程。')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    def test_send_passes_user_id_to_agent(self, mock_create, _run):
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '提醒我明天 10 点开会',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        self.assertEqual(kwargs.get('user_id'), self.alice.id)
        self.assertEqual(kwargs.get('agent_type'), 'chat')
        self.assertEqual(kwargs.get('conversation_id'), resp.data['conversation_id'])

    @patch('backend.apps.chat.views.run_agent', return_value='今天天气不错。')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    def test_common_chat_still_works(self, mock_create, _run):
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '今天天气怎么样',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['answer'], '今天天气不错。')
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        self.assertEqual(kwargs.get('user_id'), self.alice.id)


class ChatGeneratedFileTests(TransactionTestCase):
    """对话文件生成：工具调用、标记解析、下载权限、过期清理

    使用 TransactionTestCase，因为 generate_file 工具在 AgentScope 后台线程中
    通过 sync_to_async 写入数据库，与 TestCase 的事务包裹存在 SQLite 锁冲突。
    """

    def setUp(self):
        self.alice = _make_user('alice')
        self.bob = _make_user('bob')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.conv = Conversation.objects.create(user=self.alice, title='文件测试', type='chat')

    def _make_generated_file(self, owner=None, expired=False):
        owner = owner or self.alice
        f = ChatGeneratedFile.objects.create(
            owner=owner,
            conversation=self.conv,
            file_name='test.docx',
            file_type='docx',
            file_size=8,
            file=ContentFile(b'fake_docx', name='test.docx'),
            is_expired=expired,
        )
        return f

    @patch('backend.apps.common.filegen.build_docx', return_value=b'fake_docx_bytes')
    def test_generate_file_tool_creates_record(self, _mock_build):
        chunk = generate_file(
            file_type='docx',
            title='测试文档',
            content='测试内容',
            user_id=self.alice.id,
            conversation_id=self.conv.id,
        )
        # 工具是协程，需事件循环执行；Py3.10+ 起无线程默认 loop，用 asyncio.run 新建
        import asyncio
        result = asyncio.run(chunk)
        if result.state != 'success':
            self.fail(f'generate_file failed: {result.content[0].text if result.content else "no content"}')
        self.assertEqual(ChatGeneratedFile.objects.count(), 1)
        record = ChatGeneratedFile.objects.first()
        self.assertEqual(record.file_type, 'docx')
        self.assertEqual(record.owner, self.alice)
        self.assertEqual(record.conversation, self.conv)
        self.assertEqual(record.file_size, len(b'fake_docx_bytes'))
        self.assertIn('测试文档.docx', result.content[0].text)
        self.assertIn('GENERATED_FILE', result.content[0].text)

    def test_link_generated_files_updates_meta(self):
        f = self._make_generated_file()
        answer = f'已生成文件《test.docx》[GENERATED_FILE:{f.id}]，请下载。'
        msg = Message.objects.create(conversation=self.conv, role='assistant', content=answer)
        from backend.apps.chat.views import ChatViewSet

        files = ChatViewSet()._link_generated_files(answer, msg)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['id'], f.id)
        self.assertEqual(files[0]['type'], 'docx')

        f.refresh_from_db()
        self.assertEqual(f.message, msg)
        self.assertEqual(f.conversation, self.conv)

        msg.refresh_from_db()
        self.assertIn('generated_files', msg.meta)
        self.assertEqual(msg.meta['generated_files'][0]['id'], f.id)

    def test_link_generated_files_fallback_without_marker(self):
        """模型未保留 [GENERATED_FILE:id] 标记时，仍能通过时间窗口关联"""
        f = self._make_generated_file()
        answer = 'Word 文档已生成完毕，请在对话中查看。'
        msg = Message.objects.create(conversation=self.conv, role='assistant', content=answer)
        from backend.apps.chat.views import ChatViewSet

        files = ChatViewSet()._link_generated_files(answer, msg)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['id'], f.id)

        f.refresh_from_db()
        self.assertEqual(f.message, msg)
        msg.refresh_from_db()
        self.assertIn('generated_files', msg.meta)

    def test_download_own_file(self):
        f = self._make_generated_file()
        resp = self.client.get(f'/api/chat/files/{f.id}/download/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            resp['Content-Type'],
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
        self.assertIn('test.docx', resp['Content-Disposition'])

    def test_download_others_file_returns_404(self):
        f = self._make_generated_file()
        self.client.force_authenticate(user=self.bob)
        resp = self.client.get(f'/api/chat/files/{f.id}/download/')
        self.assertEqual(resp.status_code, 404)

    def test_download_expired_file_returns_410(self):
        f = self._make_generated_file(expired=True)
        resp = self.client.get(f'/api/chat/files/{f.id}/download/')
        self.assertEqual(resp.status_code, 410)

    def test_clean_chat_files_marks_expired(self):
        from datetime import timedelta
        from django.utils import timezone
        from django.core.management import call_command

        f = self._make_generated_file()
        f.created_at = timezone.now() - timedelta(days=8)
        f.save(update_fields=['created_at'])

        call_command('clean_chat_files')
        f.refresh_from_db()
        self.assertTrue(f.is_expired)


class ConversationExportTests(TestCase):
    """会话导出 md/docx：内容完整、权限隔离"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.bob = _make_user('bob')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.conv = Conversation.objects.create(user=self.alice, title='导出测试', type='chat')
        Message.objects.create(conversation=self.conv, role='user', content='你好')
        Message.objects.create(conversation=self.conv, role='assistant', content='你好呀')

    def test_export_md(self):
        resp = self.client.get(f'/api/chat/conversations/{self.conv.id}/export/?type=md')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp['Content-Type'], 'text/markdown; charset=utf-8')
        body = resp.content.decode('utf-8')
        self.assertIn('# 导出测试', body)
        self.assertIn('## 问', body)
        self.assertIn('## 答', body)
        self.assertIn('你好', body)
        self.assertIn('你好呀', body)

    def test_export_docx(self):
        resp = self.client.get(f'/api/chat/conversations/{self.conv.id}/export/?type=docx')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            resp['Content-Type'],
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
        body = resp.content
        self.assertTrue(body.startswith(b'PK'))  # docx 是 zip 格式

    def test_export_pdf(self):
        resp = self.client.get(f'/api/chat/conversations/{self.conv.id}/export/?type=pdf')
        # 本地未安装 reportlab 时返回 503，安装后应为 200
        self.assertIn(resp.status_code, (200, 503), resp.content)
        if resp.status_code == 200:
            self.assertEqual(resp['Content-Type'], 'application/pdf')
            self.assertTrue(resp.content.startswith(b'%PDF'))

    def test_export_rejects_invalid_type(self):
        resp = self.client.get(f'/api/chat/conversations/{self.conv.id}/export/?type=xyz')
        self.assertEqual(resp.status_code, 400)

    def test_export_others_conversation_returns_404(self):
        other = Conversation.objects.create(user=self.bob, title='别人的', type='chat')
        resp = self.client.get(f'/api/chat/conversations/{other.id}/export/?type=md')
        self.assertEqual(resp.status_code, 404)

    def test_export_requires_authentication(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(f'/api/chat/conversations/{self.conv.id}/export/?type=md')
        self.assertEqual(resp.status_code, 401)


class ChatStreamTests(TestCase):
    """SSE 流式对话：事件序列 + 消息落库"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    def _read_stream(self, resp):
        return b''.join(resp.streaming_content).decode('utf-8')

    @patch('backend.apps.chat.views.run_agent_stream')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_stream_events_and_persist(self, _health, _create, mock_stream):
        mock_stream.return_value = iter([
            {'type': 'chunk', 'content': '你'},
            {'type': 'chunk', 'content': '好'},
        ])
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '你好', 'stream': True,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        body = self._read_stream(resp)
        self.assertIn('event: start', body)
        self.assertIn('event: chunk', body)
        self.assertIn('event: end', body)
        self.assertIn('你', body)

        conv = Conversation.objects.get()
        self.assertEqual(conv.message_count, 2)
        answer = conv.messages.get(role='assistant')
        self.assertEqual(answer.content, '你好')

    @patch('backend.apps.chat.views.run_agent_stream')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_stream_usage_accumulated_and_persisted(self, _health, _create, mock_stream):
        """真实 token 计量：多次模型调用的 usage 累加，落助手消息并随 end 下发"""
        mock_stream.return_value = iter([
            {'type': 'chunk', 'content': '答'},
            {'type': 'usage', 'input_tokens': 120, 'output_tokens': 30,
             'cache_input_tokens': 10, 'cache_creation_input_tokens': 0,
             'model_calls': 1},
            {'type': 'usage', 'input_tokens': 80, 'output_tokens': 20,
             'cache_input_tokens': 0, 'cache_creation_input_tokens': 5,
             'model_calls': 1},
        ])
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '你好', 'stream': True,
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        body = self._read_stream(resp)
        self.assertNotIn('event: usage', body)
        self.assertIn('"input_tokens": 200', body)
        self.assertIn('"output_tokens": 50', body)

        answer = Conversation.objects.get().messages.get(role='assistant')
        self.assertEqual(answer.input_tokens, 200)
        self.assertEqual(answer.output_tokens, 50)
        self.assertEqual(answer.meta['usage']['cache_input_tokens'], 10)
        self.assertEqual(answer.meta['usage']['model_calls'], 2)

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'offline', 'error': '服务不可用'})
    def test_stream_health_fail_emits_error(self, _health):
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '你好', 'stream': True,
        }, format='json')
        body = self._read_stream(resp)
        self.assertIn('event: error', body)
        self.assertIn('服务不可用', body)
        # 健康检查失败不落消息
        self.assertEqual(Message.objects.count(), 0)

    @patch('backend.apps.chat.views.run_agent_stream')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_stream_query_limit_note_forwarded_and_persisted(self, _h, _c, mock_stream):
        """A3 触顶：正文如实句进消息、降级条既转发又落 meta"""
        mock_stream.return_value = iter([
            {'type': 'chunk', 'content': '部分结果正文'},
            {'type': 'chunk', 'content': '\n\n（已达本次查询次数上限，本轮停止继续调用，没能查到完整结果）'},
            {'type': 'degrade', 'notes': [
                {'name': '本轮检索', 'reason': '已达本次查询次数上限，本轮停止继续调用，未能查到完整结果'}]},
        ])
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '帮我查', 'stream': True,
        }, format='json')
        body = self._read_stream(resp)
        self.assertIn('event: degrade', body)
        self.assertIn('已达本次查询次数上限', body)

        answer = Conversation.objects.get().messages.get(role='assistant')
        self.assertIn('没能查到完整结果', answer.content)
        self.assertTrue(any('已达本次查询次数上限' in n
                            for n in (answer.meta or {}).get('degrade', [])))


class ChatRegenerateTests(TestCase):
    """D2 重新生成：旧版留档为「上一版」，不新增用户消息，不进上下文"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.bob = _make_user('bob')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.conv = Conversation.objects.create(user=self.alice, title='问答', type='chat')
        self.user_msg = Message.objects.create(
            conversation=self.conv, role='user', content='什么是拉锁')
        self.old = Message.objects.create(
            conversation=self.conv, role='assistant', content='旧版答案')
        self.conv.message_count = 2
        self.conv.save()

    def _read_stream(self, resp):
        return b''.join(resp.streaming_content).decode('utf-8')

    def _regen(self, payload=None):
        body = {'conversation_id': self.conv.id}
        body.update(payload or {})
        return self.client.post('/api/chat/chat/regenerate/', body, format='json')

    @patch('backend.apps.chat.views.run_agent_stream')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_previous_version_kept_and_new_appended(self, _health, _create, mock_stream):
        mock_stream.return_value = iter([{'type': 'chunk', 'content': '新版答案'}])
        resp = self._regen({'message_id': self.old.id})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('event: end', self._read_stream(resp))

        self.old.refresh_from_db()
        new = Message.objects.get(role='assistant', content='新版答案')
        self.assertTrue(self.old.meta['superseded'])
        self.assertEqual(self.old.meta['superseded_by'], new.id)
        self.assertEqual(new.meta['regenerates'], self.old.id)
        # 重新生成只多一版回答，不多一次提问
        self.assertEqual(self.conv.messages.filter(role='user').count(), 1)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.message_count, 3)

    @patch('backend.apps.chat.views.run_agent_stream')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_old_version_not_injected_into_history(self, _health, _create, mock_stream):
        captured = {}

        def fake_stream(agent, history, prompt):
            captured['history'] = history
            captured['prompt'] = prompt
            return iter([{'type': 'chunk', 'content': '新版答案'}])

        mock_stream.side_effect = fake_stream
        self._read_stream(self._regen())
        contents = [h['content'] for h in captured['history']]
        self.assertNotIn('旧版答案', contents)
        self.assertIn('什么是拉锁', contents)
        self.assertEqual(captured['prompt'], '什么是拉锁')

    @patch('backend.apps.chat.views.run_agent_stream')
    @patch('backend.apps.chat.views.create_agent_blocking', return_value=object())
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_repeat_regenerate_keeps_only_latest_current(self, _health, _create, mock_stream):
        captured = []

        def fake_stream(agent, history, prompt):
            captured.append([h['content'] for h in history])
            return iter([{'type': 'chunk', 'content': f'第{len(captured)}版答案'}])

        mock_stream.side_effect = fake_stream
        self._read_stream(self._regen())
        self._read_stream(self._regen())

        olds = list(self.conv.messages.filter(role='assistant').order_by('created_at', 'id'))
        self.assertEqual([m.content for m in olds], ['旧版答案', '第1版答案', '第2版答案'])
        self.assertEqual([bool((m.meta or {}).get('superseded')) for m in olds], [True, True, False])
        # 生成第2版时，上下文只剩提问：第1版正在被重生、旧版已是上一版
        self.assertEqual(captured[-1], ['什么是拉锁'])
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.message_count, 4)

    def test_only_latest_answer_can_be_regenerated(self):
        newer = Message.objects.create(
            conversation=self.conv, role='assistant', content='更新的一版')
        resp = self._regen({'message_id': self.old.id})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'not_latest')
        # 拒绝重生的同时不改任何数据
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.message_count, 2)
        self.assertFalse((newer.meta or {}).get('superseded'))
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 2)

    def test_latest_superseded_rejected(self):
        self.old.meta = {'superseded': True}
        self.old.save()
        resp = self._regen()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'already_superseded')

    def test_conversation_without_answer_404(self):
        empty = Conversation.objects.create(user=self.alice, title='只有一问', type='chat')
        Message.objects.create(conversation=empty, role='user', content='在吗')
        resp = self.client.post(
            '/api/chat/chat/regenerate/', {'conversation_id': empty.id}, format='json')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['code'], 'message_not_found')

    def test_other_users_conversation_404(self):
        resp = self.client.post(
            '/api/chat/chat/regenerate/', {'conversation_id': Conversation.objects.create(
                user=self.bob, title='别人的', type='chat').id}, format='json')
        self.assertEqual(resp.status_code, 404)

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'offline', 'error': '服务不可用'})
    def test_health_fail_keeps_old_version_current(self, _health):
        resp = self._regen()
        body = self._read_stream(resp)
        self.assertIn('event: error', body)
        self.old.refresh_from_db()
        self.assertFalse((self.old.meta or {}).get('superseded'))
        self.assertEqual(self.conv.messages.filter(role='assistant').count(), 1)


def _make_agent(name, owner, **overrides):
    """建一个智能体档案（D1 会话绑定用），绕开写入校验直接落库"""
    data = {'name': name, 'description': f'{name}的用途说明', 'model_name': 'qwen-max',
            'prompt': '你是一个企业助手。', 'status': AgentStatus.PUBLISHED,
            'created_by': owner, 'updated_by': owner}
    data.update(overrides)
    return AgentProfile.objects.create(**data)


class ChatAgentBindingTests(TestCase):
    """D1：一条会话只对应一个智能体；助手下线后历史只读；未选时回落出厂通用问答"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.a = _make_agent('工单助手', self.alice)
        self.b = _make_agent('合同助手', self.alice)
        self.conv = Conversation.objects.create(
            user=self.alice, title='问答', type='chat', agent=self.a)
        Message.objects.create(conversation=self.conv, role='user', content='什么是拉锁')
        Message.objects.create(conversation=self.conv, role='assistant', content='拉锁是…')

    def _send(self, **body):
        payload = {'message': '再问一句', 'conversation_id': self.conv.id}
        payload.update(body)
        return self.client.post('/api/chat/chat/send/', payload, format='json')

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_switch_agent_mid_conversation_rejected(self, _health):
        resp = self._send(agent_id=self.b.pk)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.data['code'], 'agent_locked')
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_id, self.a.pk)

    def test_admin_only_agent_rejected_for_business_user(self):
        """A6：业务用户直接引用仅管理端智能体必须被拒，不能靠前端不展示"""
        agent = _make_agent('底座助手', self.alice, visibility=Visibility.ADMIN_ONLY)
        resp = self._send(agent_id=agent.pk)
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(resp.data['code'], 'agent_admin_only')

    def test_bound_admin_only_conversation_cannot_continue(self):
        """已绑定会话的智能体改判为仅管理端后，历史可读但不能继续追问"""
        self.a.visibility = Visibility.ADMIN_ONLY
        self.a.save(update_fields=['visibility'])
        resp = self._send()
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(resp.data['code'], 'agent_admin_only')

    @patch('backend.apps.chat.views.run_profile_reply', return_value=('好的', [], {'input_tokens': 0, 'output_tokens': 0, 'cache_input_tokens': 0, 'cache_creation_input_tokens': 0, 'model_calls': 0}))
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_new_conversation_pins_opening_version(self, _health, reply):
        """7.3：新会话钉住开场版本，后续重发布/回滚不影响进行中的会话"""
        record = self.a.publish(actor=self.alice, change_summary='首版')
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '你好', 'agent_id': self.a.pk}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        conv = Conversation.objects.filter(
            user=self.alice, agent=self.a).exclude(pk=self.conv.pk).latest('id')
        self.assertEqual(conv.agent_version, record.version)
        self.assertEqual(reply.call_args.kwargs['agent_version'], record.version)

    @patch('backend.apps.chat.views.run_profile_reply', return_value=('好的', [], {'input_tokens': 0, 'output_tokens': 0, 'cache_input_tokens': 0, 'cache_creation_input_tokens': 0, 'model_calls': 0}))
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_same_agent_id_still_allowed(self, _health, _reply):
        """同一条会话重复带上已绑定的智能体不算换助手"""
        resp = self._send(agent_id=self.a.pk)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_id, self.a.pk)

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_offline_agent_conversation_readonly(self, _health):
        self.a.status = AgentStatus.DISABLED
        self.a.save(update_fields=['status'])
        resp = self._send()
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.data['code'], 'agent_offline')
        self.assertEqual(self.conv.messages.filter(role='user').count(), 1)

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_offline_agent_blocks_regenerate(self, _health):
        self.a.status = AgentStatus.ARCHIVED
        self.a.save(update_fields=['status'])
        resp = self.client.post(
            '/api/chat/chat/regenerate/', {'conversation_id': self.conv.id}, format='json')
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.data['code'], 'agent_offline')

    def test_conversation_list_exposes_agent_status(self):
        self.a.status = AgentStatus.DISABLED
        self.a.save(update_fields=['status'])
        resp = self.client.get('/api/chat/conversations/')
        row = next(x for x in resp.data['results'] if x['id'] == self.conv.id)
        self.assertEqual(row['agent'], self.a.pk)
        self.assertEqual(row['agent_status'], 'disabled')

    def test_binding_gate_passes_normal_cases(self):
        view = ChatViewSet()
        self.assertIsNone(view._check_agent_binding(self.alice, self.conv.pk, self.a))
        self.assertIsNone(view._check_agent_binding(self.alice, self.conv.pk, None))
        free = Conversation.objects.create(user=self.alice, title='自由问', type='chat')
        self.assertIsNone(view._check_agent_binding(self.alice, free.pk, self.b))

    def test_new_chat_falls_back_to_factory_agent(self):
        default = _make_agent('通用问答', self.alice, is_builtin=True, status=AgentStatus.PUBLISHED)
        conv = ChatViewSet()._get_or_create_conversation(
            self.alice, None, 'chat', None, '你好')
        self.assertEqual(conv.agent_id, default.pk)

    def test_factory_fallback_skips_other_conversation_types(self):
        _make_agent('通用问答', self.alice, is_builtin=True, status=AgentStatus.PUBLISHED)
        conv = ChatViewSet()._get_or_create_conversation(
            self.alice, None, 'knowledge', 7, '你好')
        self.assertIsNone(conv.agent_id)

    def test_no_factory_agent_keeps_platform_generic_path(self):
        """内置件没安装时不硬绑，仍走平台通用问答"""
        _make_agent('通用问答', self.alice, is_builtin=True, status=AgentStatus.DRAFT)
        conv = ChatViewSet()._get_or_create_conversation(
            self.alice, None, 'chat', None, '你好')
        self.assertIsNone(conv.agent_id)

    def test_explicit_agent_wins_over_factory(self):
        _make_agent('通用问答', self.alice, is_builtin=True, status=AgentStatus.PUBLISHED)
        conv = ChatViewSet()._get_or_create_conversation(
            self.alice, None, 'chat', None, '你好', agent=self.a)
        self.assertEqual(conv.agent_id, self.a.pk)


class ConversationSwitchAgentTests(TestCase):
    """P1-c：会话内切换助手 = 新建会话承接历史，旧会话只读；开关默认关闭"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.bob = _make_user('bob')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.a = _make_agent('工单助手', self.alice)
        self.b = _make_agent('合同助手', self.alice)
        self.conv = Conversation.objects.create(
            user=self.alice, title='问答', type='chat', agent=self.a,
            knowledge_base_ids=[3],
        )
        self.msg1 = Message.objects.create(
            conversation=self.conv, role='user', content='什么是拉锁')
        self.msg2 = Message.objects.create(
            conversation=self.conv, role='assistant', content='拉锁是…')
        self.url = f'/api/chat/conversations/{self.conv.id}/switch-agent/'

    def _enable(self, value='true'):
        SystemSetting.set_value('chat_agent_switch_enabled', value)

    def test_switch_disabled_by_default(self):
        resp = self.client.post(self.url, {'agent_id': self.b.pk}, format='json')
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(resp.data['code'], 'agent_switch_disabled')
        self.assertFalse(Conversation.objects.filter(switched_from=self.conv).exists())

    def test_policy_endpoint_reports_flag(self):
        resp = self.client.get('/api/chat/conversations/agent-switch-policy/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data['enabled'])
        self._enable()
        resp = self.client.get('/api/chat/conversations/agent-switch-policy/')
        self.assertTrue(resp.data['enabled'])

    def test_switch_creates_successor_and_copies_history(self):
        self._enable()
        resp = self.client.post(self.url, {'agent_id': self.b.pk}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['agent'], self.b.pk)
        self.assertEqual(resp.data['switched_from'], self.conv.id)
        self.assertEqual(resp.data['switched_from_title'], '问答')
        self.assertEqual(resp.data['message_count'], 2)
        self.assertFalse(resp.data['is_readonly'])

        new_conv = Conversation.objects.get(pk=resp.data['id'])
        self.assertEqual(new_conv.agent_id, self.b.pk)
        self.assertEqual(new_conv.type, 'chat')
        self.assertEqual(new_conv.knowledge_base_ids, [3])
        copied = list(new_conv.messages.order_by('created_at', 'id'))
        self.assertEqual([m.role for m in copied], ['user', 'assistant'])
        self.assertEqual([m.content for m in copied], ['什么是拉锁', '拉锁是…'])
        # 历史时间线原样承接，不能全部变成切换时刻
        self.assertEqual([m.created_at for m in copied],
                         [self.msg1.created_at, self.msg2.created_at])
        # 旧会话的消息不被搬走，绑定也不被改写
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_id, self.a.pk)
        self.assertEqual(self.conv.messages.count(), 2)

    def test_old_conversation_readonly_in_list(self):
        self._enable()
        self.client.post(self.url, {'agent_id': self.b.pk}, format='json')
        resp = self.client.get('/api/chat/conversations/')
        rows = {row['id']: row for row in resp.data['results']}
        self.assertTrue(rows[self.conv.id]['is_readonly'])
        new_id = Conversation.objects.get(switched_from=self.conv).id
        self.assertFalse(rows[new_id]['is_readonly'])

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_old_conversation_cannot_continue(self, _health):
        self._enable()
        self.client.post(self.url, {'agent_id': self.b.pk}, format='json')
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '再问一句', 'conversation_id': self.conv.id}, format='json')
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.data['code'], 'conversation_readonly')
        self.assertEqual(self.conv.messages.count(), 2)

    def test_switch_to_admin_only_agent_forbidden(self):
        self._enable()
        agent = _make_agent('底座助手', self.alice, visibility=Visibility.ADMIN_ONLY)
        resp = self.client.post(self.url, {'agent_id': agent.pk}, format='json')
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(resp.data['code'], 'agent_admin_only')

    def test_switch_to_unpublished_agent_rejected(self):
        self._enable()
        draft = _make_agent('草稿助手', self.alice, status=AgentStatus.DRAFT)
        resp = self.client.post(self.url, {'agent_id': draft.pk}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.data['code'], 'agent_unavailable')

    def test_switch_to_same_agent_rejected(self):
        self._enable()
        resp = self.client.post(self.url, {'agent_id': self.a.pk}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.data['code'], 'agent_same')

    def test_switch_requires_agent_id(self):
        self._enable()
        resp = self.client.post(self.url, {}, format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.data['code'], 'agent_required')

    def test_switch_others_conversation_404(self):
        self._enable()
        other = Conversation.objects.create(
            user=self.bob, title='别人的', type='chat', agent=self.a)
        resp = self.client.post(
            f'/api/chat/conversations/{other.id}/switch-agent/',
            {'agent_id': self.b.pk}, format='json')
        self.assertEqual(resp.status_code, 404, resp.content)

    def test_switch_old_conversation_twice_rejected(self):
        self._enable()
        self.client.post(self.url, {'agent_id': self.b.pk}, format='json')
        resp = self.client.post(self.url, {'agent_id': self.b.pk}, format='json')
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.data['code'], 'conversation_readonly')


class KnowledgePolicyChatE3Tests(TestCase):
    """E3：按需查不预先检索、每轮必查先检索、追问补全改写检索问题"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.agent = _make_agent('知识助手', self.alice, knowledge_base_ids=[11])
        self.conv = Conversation.objects.create(
            user=self.alice, title='问答', type='chat', agent=self.agent,
            knowledge_base_ids=[11])
        Message.objects.create(conversation=self.conv, role='user', content='报销流程是什么')
        Message.objects.create(conversation=self.conv, role='assistant', content='先填单再审批')

    def _send(self, message='再问一句', **extra):
        payload = {'message': message, 'conversation_id': self.conv.id}
        payload.update(extra)
        return self.client.post('/api/chat/chat/send/', payload, format='json')

    @patch('backend.apps.chat.views.standalone_question', return_value='那它的价格呢')
    @patch('backend.apps.chat.views.run_profile_reply', return_value=('好的', [], {'input_tokens': 0, 'output_tokens': 0, 'cache_input_tokens': 0, 'cache_creation_input_tokens': 0, 'model_calls': 0}))
    @patch('backend.apps.chat.views.multi_kb_search', return_value=[])
    def test_on_demand_skips_prefetch(self, search, _reply, _rewrite):
        """默认按需查：平台不预先检索，是否查库交给模型判断"""
        resp = self._send('那它的价格呢')
        self.assertEqual(resp.status_code, 200, resp.content)
        search.assert_not_called()
        self.assertEqual(resp.data['sources'], [])

    @patch('backend.apps.chat.views.standalone_question', return_value='报销标准是多少')
    @patch('backend.apps.chat.views.run_profile_reply', return_value=('好的', [], {'input_tokens': 0, 'output_tokens': 0, 'cache_input_tokens': 0, 'cache_creation_input_tokens': 0, 'model_calls': 0}))
    @patch('backend.apps.chat.views.multi_kb_search', return_value=[])
    def test_always_prefetches_before_answer(self, search, _reply, _rewrite):
        self.agent.knowledge_mode = 'always'
        self.agent.save(update_fields=['knowledge_mode'])
        resp = self._send('报销标准是多少')
        self.assertEqual(resp.status_code, 200, resp.content)
        search.assert_called_once()
        self.assertEqual(search.call_args.args[0], [11])

    @patch('backend.apps.chat.views.run_profile_reply', return_value=('好的', [], {'input_tokens': 0, 'output_tokens': 0, 'cache_input_tokens': 0, 'cache_creation_input_tokens': 0, 'model_calls': 0}))
    @patch('backend.apps.chat.views.multi_kb_search', return_value=[])
    def test_tool_hits_become_sources_and_persist(self, _search, _reply):
        """按需查下引用来源来自模型真实调用检索工具的结果，并随消息留档"""
        hits = [{
            'kb_id': 11, 'kb_name': '制度库', 'doc_title': '报销制度',
            'file_name': 'baoxiao.pdf', 'score': 0.8123, 'chunk_id': 5,
            'doc_id': 3, 'chunk_index': 0, 'content': '差旅报销需先填单',
        }]

        def fake_reply(agent_id, history, message, **kwargs):
            kwargs['on_knowledge_sources'](hits)
            return '按制度需先填单', [], {
                'input_tokens': 0, 'output_tokens': 0,
                'cache_input_tokens': 0, 'cache_creation_input_tokens': 0,
                'model_calls': 0,
            }

        with patch('backend.apps.chat.views.run_profile_reply', side_effect=fake_reply):
            resp = self._send('报销要填单吗')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data['sources']), 1)
        self.assertEqual(resp.data['sources'][0]['title'], '报销制度')
        self.assertEqual(resp.data['sources'][0]['score'], 0.81)

        msg = Message.objects.filter(
            conversation=self.conv, role='assistant').latest('id')
        self.assertEqual(msg.meta['sources'][0]['knowledge_base_name'], '制度库')

    @patch('backend.apps.chat.views.standalone_question', return_value='差旅报销的标准是多少')
    @patch('backend.apps.chat.views.run_profile_reply', return_value=('好的', [], {'input_tokens': 0, 'output_tokens': 0, 'cache_input_tokens': 0, 'cache_creation_input_tokens': 0, 'model_calls': 0}))
    @patch('backend.apps.chat.views.multi_kb_search', return_value=[])
    def test_followup_rewrite_feeds_retrieval(self, search, reply, rewrite):
        """追问补全：省略式追问先补成完整问题再检索，答案仍按原问题落库"""
        self.agent.knowledge_mode = 'always'
        self.agent.save(update_fields=['knowledge_mode'])
        resp = self._send('那它的价格呢')
        self.assertEqual(resp.status_code, 200, resp.content)
        rewrite.assert_called_once()
        self.assertEqual(search.call_args.args[1], '差旅报销的标准是多少')
        self.assertIn('差旅报销的标准是多少', reply.call_args.args[2])
        self.assertEqual(
            Message.objects.filter(conversation=self.conv, role='user').latest('id').content,
            '那它的价格呢')

    @patch('backend.apps.chat.views.standalone_question', return_value='不该被调用')
    @patch('backend.apps.chat.views.run_profile_reply', return_value=('好的', [], {'input_tokens': 0, 'output_tokens': 0, 'cache_input_tokens': 0, 'cache_creation_input_tokens': 0, 'model_calls': 0}))
    @patch('backend.apps.chat.views.multi_kb_search', return_value=[])
    def test_followup_rewrite_can_be_disabled(self, _search, _reply, rewrite):
        self.agent.followup_rewrite = False
        self.agent.knowledge_mode = 'always'
        self.agent.save(update_fields=['followup_rewrite', 'knowledge_mode'])
        self._send('那它的价格呢')
        rewrite.assert_not_called()


class NeedsRewriteHelperTests(SimpleTestCase):
    """E3：追问补全的触发条件只认指代/省略信号，避免每轮多付一次改写调用"""

    HISTORY = [{'role': 'user', 'content': '报销流程是什么'},
               {'role': 'assistant', 'content': '先填单再审批'}]

    def test_pronoun_followup_needs_rewrite(self):
        self.assertTrue(needs_rewrite('那它的价格呢', self.HISTORY))
        self.assertTrue(needs_rewrite('这个怎么弄', self.HISTORY))

    def test_complete_question_skipped(self):
        self.assertFalse(needs_rewrite('差旅报销标准是多少', self.HISTORY))

    def test_first_turn_skipped(self):
        self.assertFalse(needs_rewrite('那它的价格呢', []))


class TrialChatTests(TestCase):
    """A3 试聊：草稿配置装配、不留会话、权限口径、工具留痕标「试聊」"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.bob = _make_user('bob')
        self.admin = User.objects.create_user(username='root', password='demo-pass-9x2',
                                              is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.agent = _make_agent('工单助手', self.alice, status=AgentStatus.DRAFT)

    def _trial(self, user=None, **body):
        if user is not None:
            self.client.force_authenticate(user=user)
        payload = {'agent_id': self.agent.pk, 'message': '你好'}
        payload.update(body)
        return self.client.post('/api/chat/chat/trial/', payload, format='json')

    @staticmethod
    def _read_stream(resp):
        """流式应答必须消费后断言：既拿到 body，也驱动 side_effect 记录 capture"""
        return b''.join(resp.streaming_content).decode('utf-8')

    @staticmethod
    def _fake_stream(events, capture):
        """替换 run_profile_stream：记录入参并按脚本产出事件"""
        def gen(agent_id, history, message, **kwargs):
            capture.update(kwargs)
            capture['call'] = (agent_id, list(history), message)
            yield from events
        return gen

    def test_empty_message_rejected(self):
        resp = self._trial(message='  ')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'empty_message')

    def test_agent_required_and_must_exist(self):
        resp = self._trial(agent_id=None)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'agent_required')
        resp = self._trial(agent_id=99999)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['code'], 'agent_not_found')

    @patch('backend.apps.chat.views.run_profile_stream',
           side_effect=lambda *a, **k: iter([]))
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_only_editor_can_trial(self, _health, _stream):
        """他人不可试聊别人的草稿；管理员可以"""
        resp = self._trial(user=self.bob)
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(resp.data['code'], 'forbidden')
        resp = self._trial(user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self._read_stream(resp)

    @patch('backend.apps.chat.views.run_profile_stream',
           side_effect=lambda *a, **k: iter([]))
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_admin_can_trial_admin_only_agent(self, _health, _stream):
        """A6：仅管理端智能体管理员仍可试聊（装配面权限不受可见范围影响）"""
        self.agent.visibility = Visibility.ADMIN_ONLY
        self.agent.save(update_fields=['visibility'])
        resp = self._trial(user=self.admin)
        self.assertEqual(resp.status_code, 200)
        self._read_stream(resp)

    def test_health_offline_returns_503(self):
        with patch('backend.apps.chat.views.check_health',
                   return_value={'status': 'error', 'error': '凭据无效'}):
            resp = self._trial()
        self.assertEqual(resp.status_code, 503)
        self.assertIn('模型服务', resp.data['error'])

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_trial_streams_without_persisting(self, _health):
        capture = {}
        events = [
            {'type': 'degrade', 'notes': [{'kind': 'mcp', 'name': '工单系统',
                                           'reason': '来源异常，本轮不可用'}]},
            {'type': 'chunk', 'content': '你好，'},
            {'type': 'chunk', 'content': '我是工单助手'},
        ]
        with patch('backend.apps.chat.views.run_profile_stream',
                   side_effect=self._fake_stream(events, capture)):
            resp = self._trial(message='你好')
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp['Content-Type'], 'text/event-stream')
            # 流式生成器在消费时才执行，必须在 patch 生效期间读完整段
            body = self._read_stream(resp)
        self.assertIn('我是工单助手', body)
        self.assertIn('来源异常', body)
        self.assertIn('"type": "done"', body)
        # 草稿路径 + 不留会话/消息
        self.assertTrue(capture['draft'])
        self.assertTrue(callable(capture['on_tool']))
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(Message.objects.count(), 0)

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_trial_history_sanitized_and_trimmed(self, _health):
        capture = {}
        junk = ([{'role': 'system', 'content': '越权指令'}]
                + ['不是字典'] * 3
                + [{'role': 'user', 'content': f'第{i}问'} for i in range(25)])
        with patch('backend.apps.chat.views.run_profile_stream',
                   side_effect=self._fake_stream([], capture)):
            resp = self._trial(messages=junk)
            self.assertEqual(resp.status_code, 200)
            self._read_stream(resp)
        history = capture['call'][1]
        self.assertEqual(len(history), 20)
        self.assertTrue(all(m['role'] in ('user', 'assistant') for m in history))

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_trial_tool_calls_audited_as_trial(self, _health):
        capture = {}
        events = [{'type': 'chunk', 'content': '查证中'}]
        with patch('backend.apps.chat.views.run_profile_stream',
                   side_effect=self._fake_stream(events, capture)):
            resp = self._trial(message='帮我查工单')
            self._read_stream(resp)
        capture['on_tool']({'name': 'mcp__ticket__list_tickets', 'display': '查询工单列表',
                             'state': 'success', 'args': '{"status": "open"}',
                             'result': '[…]', 'elapsed': 0.42})
        log = AuditLog.objects.filter(action='trial_tool_call').first()
        self.assertIsNotNone(log)
        self.assertIn('工单助手', log.target)
        self.assertIn('查询工单列表', log.target)
        detail = json.loads(log.detail)
        self.assertEqual(detail['source'], '试聊')
        self.assertEqual(detail['agent'], '工单助手')
        self.assertEqual(detail['tool'], 'mcp__ticket__list_tickets')

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_trial_error_event_ends_stream(self, _health):
        capture = {}
        events = [{'type': 'chunk', 'content': '前半段'},
                  {'type': 'error', 'message': '模型超时'},
                  {'type': 'chunk', 'content': '不该出现'}]
        with patch('backend.apps.chat.views.run_profile_stream',
                   side_effect=self._fake_stream(events, capture)):
            resp = self._trial()
            body = self._read_stream(resp)
        self.assertIn('前半段', body)
        self.assertIn('模型超时', body)
        self.assertNotIn('不该出现', body)
        self.assertNotIn('"type": "done"', body)


class ModelApiTests(TestCase):
    """模型健康检查与配置接口"""

    def setUp(self):
        from backend.apps.common import llm
        from backend.apps.settings.models import SystemSetting

        self.llm = llm
        self.llm.reset_client()
        self.addCleanup(self.llm.reset_client)
        self.settings_model = SystemSetting
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok', 'model_count': 3})
    def test_health(self, _health):
        resp = self.client.get('/api/chat/models/health/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], 'ok')

    def test_config_hides_host(self):
        self.settings_model.set_value('llm_base_url', 'https://api.example.com/v1')
        self.settings_model.set_value('llm_model', 'db-model')
        resp = self.client.get('/api/chat/models/config/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('***', resp.data['base_url_display'])
        self.assertEqual(resp.data['model'], 'db-model')

    def test_config_without_db_says_not_configured(self):
        """数据库无配置时如实返回未配置，不回落到环境变量"""
        resp = self.client.get('/api/chat/models/config/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['base_url_display'], '未配置')
        self.assertEqual(resp.data['model'], '')

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get('/api/chat/conversations/').status_code, 401)


class ToolBodyStorageTests(TestCase):
    """PRD 9.2 #4：工具返回全文独立入库，摘要口径不变，失败不影响对话"""

    def setUp(self):
        self.user = _make_user('alice')
        self.conv = Conversation.objects.create(user=self.user, title='排查工单')

    def _auditor(self):
        return ChatViewSet()._make_tool_auditor(self.user, self.conv)

    def _payload(self, **overrides):
        payload = {
            'type': 'tool_result', 'call_id': 'c1',
            'name': 'mcp__ticket__list_tickets', 'display': '查询工单列表',
            'state': 'success', 'args': '{"status": "open"}',
            'result': '摘要', 'elapsed': 0.3, 'egress': False,
        }
        payload.update(overrides)
        return payload

    def test_full_body_stored_with_meta(self):
        raw = '第一行\n第二行\n' + '数据' * 400
        self._auditor()(self._payload(result=raw[:500]), raw)
        log = AuditLog.objects.get(action='tool_call')
        body = log.tool_body
        self.assertEqual(body.result, raw)
        self.assertEqual(body.result_chars, len(raw))
        self.assertEqual(body.result_sha256,
                         hashlib.sha256(raw.encode('utf-8')).hexdigest())
        self.assertFalse(body.storage_truncated)
        self.assertEqual(body.conversation_id, self.conv.id)
        self.assertEqual(body.call_id, 'c1')
        self.assertEqual(body.tool, 'mcp__ticket__list_tickets')

    def test_oversized_body_truncated_but_meta_kept(self):
        raw = '字' * (TOOL_BODY_MAX_CHARS + 100)
        self._auditor()(self._payload(), raw)
        body = AuditLog.objects.get(action='tool_call').tool_body
        self.assertEqual(len(body.result), TOOL_BODY_MAX_CHARS)
        self.assertEqual(body.result_chars, len(raw))
        self.assertTrue(body.storage_truncated)
        self.assertEqual(body.result_sha256,
                         hashlib.sha256(raw.encode('utf-8')).hexdigest())

    def test_detail_not_cut_at_2000(self):
        long_args = 'a' * 2500
        self._auditor()(self._payload(args=long_args), 'x')
        log = AuditLog.objects.get(action='tool_call')
        detail = json.loads(log.detail)
        self.assertEqual(detail['args'], long_args)
        self.assertGreater(len(log.detail), 2000)

    def test_body_failure_degrades_to_audit_only(self):
        with patch('backend.apps.chat.views.ToolCallBody.objects.create',
                   side_effect=RuntimeError('boom')):
            self._auditor()(self._payload(), '全文')
        self.assertTrue(AuditLog.objects.filter(action='tool_call').exists())
        self.assertFalse(ToolCallBody.objects.exists())

    def test_trial_tool_call_stores_body_without_conversation(self):
        from backend.apps.chat.views import _make_trial_auditor

        event = self._payload()
        _make_trial_auditor(self.user, '工单助手')(event, '试聊全文\n第二行')
        log = AuditLog.objects.get(action='trial_tool_call')
        body = log.tool_body
        self.assertEqual(body.result, '试聊全文\n第二行')
        self.assertIsNone(body.conversation_id)
        self.assertEqual(body.tool, 'mcp__ticket__list_tickets')
        self.assertEqual(log.agent_name, '工单助手')

    def test_breakdown_snapshots_written_on_tool_call(self):
        """F4 完整版：来源名与耗时落成一等列，供三维度直接聚合"""
        self._auditor()(self._payload(display='工单系统·list_tickets'), '')
        log = AuditLog.objects.get(action='tool_call')
        self.assertEqual(log.source_name, '工单系统')
        self.assertEqual(log.elapsed_ms, 300)  # 桥接层 0.3 秒 → 300ms

    def test_builtin_tool_marked_platform_source_without_elapsed(self):
        self._auditor()(self._payload(display='知识库检索', name='kb_search', elapsed=None), '')
        log = AuditLog.objects.get(action='tool_call')
        self.assertEqual(log.source_name, '平台内置')
        self.assertIsNone(log.elapsed_ms)


class CleanToolBodiesTests(TestCase):
    """PRD 9.2 #4：全文保留 90 天，到期分片硬删除，可反复执行"""

    def setUp(self):
        self.user = _make_user('cleaner')

    def _body(self, days_ago):
        log = AuditLog.objects.create(actor=self.user, action='tool_call',
                                      target='清理用例', detail='{}')
        row = ToolCallBody.objects.create(
            audit=log, call_id='c1', tool='mcp__demo__ping',
            result='x', result_chars=1)
        # created_at 是 auto_now_add，用 update 回填历史时间
        ToolCallBody.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago))
        return row

    def _run(self, **kwargs):
        out = io.StringIO()
        call_command('clean_tool_bodies', stdout=out, **kwargs)
        return out.getvalue()

    def test_retention_boundary(self):
        keep = self._body(89)
        edge = self._body(90)
        old = self._body(91)
        out = self._run()
        self.assertTrue(ToolCallBody.objects.filter(pk=keep.pk).exists())
        self.assertFalse(ToolCallBody.objects.filter(pk__in=[edge.pk, old.pk]).exists())
        self.assertIn('命中 2 条', out)

    def test_batch_deletion_across_chunks(self):
        for i in range(7):
            self._body(120 + i)
        out = self._run(batch=2)
        self.assertEqual(ToolCallBody.objects.count(), 0)
        self.assertIn('已删除 7 条', out)

    def test_dry_run_deletes_nothing(self):
        self._body(91)
        self._body(200)
        row = self._body(95)
        out = self._run(dry_run=True)
        self.assertEqual(ToolCallBody.objects.count(), 3)
        self.assertTrue(ToolCallBody.objects.filter(pk=row.pk).exists())
        self.assertIn('未删除任何行', out)
        self.assertIn('命中 3 条', out)
        self.assertIn('最早', out)

    def test_second_run_deletes_zero(self):
        self._body(100)
        self._run()
        out = self._run()
        self.assertEqual(ToolCallBody.objects.count(), 0)
        self.assertIn('已删除 0 条', out)
        self.assertIn('命中 0 条', out)

    def test_non_positive_days_rejected(self):
        with self.assertRaises(CommandError):
            self._run(days=0)
        with self.assertRaises(CommandError):
            self._run(batch=0)


class CronScriptTests(SimpleTestCase):
    """B2：部署脚本不得再引用不存在的管理命令（此前 clean_conversions 已失联）"""

    def setUp(self):
        self.path = (Path(__file__).resolve().parents[3]
                     / 'deploy' / 'ops' / 'install_cron.sh')
        self.text = self.path.read_text(encoding='utf-8')

    def test_referenced_commands_exist(self):
        from django.core.management import get_commands

        referenced = set(re.findall(r'manage\.py\s+([a-z_][a-z0-9_]*)', self.text))
        self.assertTrue(referenced)
        missing = referenced - set(get_commands())
        self.assertEqual(missing, set(), f'部署脚本引用了不存在的管理命令: {missing}')

    def test_both_cleanups_scheduled_separately(self):
        self.assertIn('clean_chat_files', self.text)
        self.assertIn('clean_tool_bodies', self.text)
        self.assertIn('0 3 * * *', self.text)
        self.assertIn('10 3 * * *', self.text)

    def _bash(self):
        found = shutil.which('bash')
        if found:
            return found
        # Windows 下 Git Bash 常不在 PATH，按常见安装位置兜底
        for candidate in (r'C:\Program Files\Git\bin\bash.exe',
                          r'C:\Program Files\Git\usr\bin\bash.exe',
                          '/bin/bash', '/usr/bin/bash'):
            if Path(candidate).exists():
                return candidate
        return ''

    def test_bash_syntax(self):
        bash = self._bash()
        if not bash:
            self.skipTest('当前环境没有 bash，跳过语法检查')
        proc = subprocess.run([bash, '-n'], input=self.text.encode('utf-8'),
                              capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))


# ─── D7 逐次询问授权 ───

def _d7_tool_call(name='mcp__ticket__list_tickets', call_id='call-1'):
    """构造 AgentScope 的待确认工具调用（真实类型，走真实桥接分支）"""
    from agentscope.message import ToolCallBlock
    from agentscope.permission import PermissionBehavior, PermissionRule

    return ToolCallBlock(
        id=call_id,
        name=name,
        input='{"status": "open"}',
        suggested_rules=[PermissionRule(
            tool_name=name, rule_content='',
            behavior=PermissionBehavior.ALLOW, source='user')],
    )


class _D7FakeAgent:
    """第一轮要求确认，收到 UserConfirmResultEvent 后第二轮吐出正文"""

    def __init__(self):
        self.rounds = []

    async def reply_stream(self, inputs):
        self.rounds.append(inputs)
        if len(self.rounds) == 1:
            from agentscope.event import RequireUserConfirmEvent
            yield RequireUserConfirmEvent(
                reply_id='reply-1', tool_calls=[_d7_tool_call()])
        else:
            from agentscope.event import ReplyEndEvent, TextBlockDeltaEvent
            yield TextBlockDeltaEvent(
                reply_id='reply-1', block_id='block-1', delta='已处理')
            yield ReplyEndEvent(session_id='session-1', reply_id='reply-1')


class PermissionSessionTests(TransactionTestCase):
    """D7：待决授权落库注册表（跨线程决定、超时、迟到、重启失效）"""

    def setUp(self):
        self.user = _make_user('perm_owner')

    @staticmethod
    async def _open(session):
        return await session.open({
            'call_id': 'call-1',
            'tool_name': 'list_tickets',
            'display': '工单系统·list_tickets',
        })

    def test_allow_once_resolved_from_another_thread(self):
        async def scenario():
            session = PermissionSession(user_id=self.user.id, timeout=2)
            rid = await self._open(session)
            results = []

            def decide():
                time.sleep(0.01)
                results.append(resolve_permission(rid, self.user.id, 'allow_once'))

            threading.Thread(target=decide).start()
            return await session.wait(rid), results

        decision, results = asyncio.run(scenario())
        self.assertEqual(decision, 'allow_once')
        self.assertEqual(results, ['ok'])

    def test_deny_resolved_from_another_thread(self):
        async def scenario():
            session = PermissionSession(user_id=self.user.id, timeout=2)
            rid = await self._open(session)

            def decide():
                time.sleep(0.01)
                resolve_permission(rid, self.user.id, 'deny')

            threading.Thread(target=decide).start()
            return await session.wait(rid)

        self.assertEqual(asyncio.run(scenario()), 'deny')

    def test_wait_times_out_without_decision(self):
        async def scenario():
            session = PermissionSession(user_id=self.user.id, timeout=0.05)
            rid = await self._open(session)
            return await session.wait(rid)

        self.assertEqual(asyncio.run(scenario()), 'timeout')

    def test_late_decision_after_timeout_is_expired(self):
        async def scenario():
            session = PermissionSession(user_id=self.user.id, timeout=0.05)
            rid = await self._open(session)
            first = await session.wait(rid)
            late = await asyncio.to_thread(
                resolve_permission, rid, self.user.id, 'allow_once')
            return first, late

        self.assertEqual(asyncio.run(scenario()), ('timeout', 'expired'))

    def test_second_decision_is_already_resolved(self):
        async def scenario():
            session = PermissionSession(user_id=self.user.id, timeout=2)
            rid = await self._open(session)
            first = await asyncio.to_thread(
                resolve_permission, rid, self.user.id, 'allow_once')
            second = await asyncio.to_thread(
                resolve_permission, rid, self.user.id, 'deny')
            return first, second, await session.wait(rid)

        self.assertEqual(
            asyncio.run(scenario()), ('ok', 'already_resolved', 'allow_once'))

    def test_other_user_cannot_resolve(self):
        async def scenario():
            session = PermissionSession(user_id=self.user.id, timeout=2)
            rid = await self._open(session)
            other = await asyncio.to_thread(
                resolve_permission, rid, 99, 'allow_once')
            owner = await asyncio.to_thread(
                resolve_permission, rid, self.user.id, 'deny')
            return other, owner, await session.wait(rid)

        self.assertEqual(
            asyncio.run(scenario()), ('not_found', 'ok', 'deny'))

    def test_request_of_previous_process_is_restarted(self):
        from backend.apps.chat import permissions
        from backend.apps.chat.models import PendingPermission

        user = _make_user('perm_restart')
        record = PendingPermission.objects.create(
            request_id='old-boot', user=user, tool_name='list_tickets',
            display='工单系统·list_tickets', instance_id='previous-boot',
            expires_at=timezone.now() + timedelta(seconds=60),
        )

        async def scenario():
            session = PermissionSession(user_id=user.id, timeout=2)
            return await session.wait(record.request_id)

        self.assertEqual(asyncio.run(scenario()), 'restarted')
        self.assertEqual(
            resolve_permission('old-boot', user.id, 'allow_once'), 'restarted')
        self.assertEqual(
            permissions.INSTANCE_ID == record.instance_id, False)


class PermissionBridgeTests(TransactionTestCase):
    """D7：bridge 真实挂起—发卡—续跑链路（真实 PermissionSession）"""

    def setUp(self):
        self.user = _make_user('perm_bridge')

    def _run(self, decision=None, *, timeout=2, with_session=True):
        from backend.apps.agents.core.bridge import _ToolTracker, _pump_stream

        payloads, granted = [], []
        agent = _D7FakeAgent()

        async def scenario():
            session = None
            if with_session:
                session = PermissionSession(
                    user_id=self.user.id, timeout=timeout, on_grant=granted.append)

            async def emit(payload):
                payloads.append(payload)
                if payload.get('type') == 'permission_request' and decision:
                    # 模拟 DRF 决定接口：在独立线程中改库
                    threading.Timer(
                        0.01, resolve_permission,
                        args=(payload['request_id'], self.user.id, decision),
                    ).start()

            tracker = _ToolTracker(displays={
                'mcp__ticket__list_tickets': '工单系统·list_tickets'})
            await _pump_stream(agent, [], emit,
                               tracker=tracker, permission_session=session)

        asyncio.run(scenario())
        return payloads, agent, granted

    def test_allow_once_resumes_with_confirmed_result(self):
        payloads, agent, granted = self._run('allow_once')
        kinds = [p['type'] for p in payloads]
        self.assertEqual(kinds, ['permission_request', 'permission_result', 'chunk'])

        request = payloads[0]
        self.assertEqual(request['tool_name'], 'list_tickets')
        self.assertEqual(request['source_name'], '工单系统')
        self.assertEqual(request['display'], '工单系统·list_tickets')
        self.assertEqual(request['timeout_seconds'], 2)
        self.assertTrue(request['can_always'])
        self.assertTrue(request['request_id'])
        self.assertEqual(payloads[1]['decision'], 'allow_once')
        self.assertEqual(payloads[2]['content'], '已处理')

        result = agent.rounds[1].confirm_results[0]
        self.assertTrue(result.confirmed)
        self.assertIsNone(result.rules)
        self.assertEqual(granted, [])

    def test_deny_resumes_with_rejected_result(self):
        payloads, agent, granted = self._run('deny')
        self.assertEqual(payloads[1]['decision'], 'deny')
        self.assertFalse(agent.rounds[1].confirm_results[0].confirmed)
        self.assertEqual(granted, [])
        # 拒绝后本轮仍继续作答
        self.assertEqual(payloads[-1], {'type': 'chunk', 'content': '已处理'})

    def test_allow_session_remembers_tool(self):
        payloads, agent, granted = self._run('allow_session')
        result = agent.rounds[1].confirm_results[0]
        self.assertTrue(result.confirmed)
        self.assertIsNotNone(result.rules)
        self.assertEqual(granted, ['mcp__ticket__list_tickets'])

    def test_without_session_no_card_and_denies(self):
        payloads, agent, _ = self._run(with_session=False)
        self.assertNotIn('permission_request', [p['type'] for p in payloads])
        self.assertEqual(payloads[0]['decision'], 'deny')
        self.assertFalse(agent.rounds[1].confirm_results[0].confirmed)

    def test_keepalive_emits_pings(self):
        from backend.apps.agents.core.bridge import _keepalive

        async def scenario():
            hits = []

            async def emit(payload):
                hits.append(payload)

            task = asyncio.create_task(_keepalive(emit, interval=0.01))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return hits

        hits = asyncio.run(scenario())
        self.assertGreaterEqual(len(hits), 2)
        self.assertTrue(all(h == {'type': 'keepalive'} for h in hits))


class PermissionViewTests(TestCase):
    """D7：SSE 转发、消息留痕、刷新降级、试聊与决定接口"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.agent = _make_agent('工单助手', self.alice)
        self.conv = Conversation.objects.create(
            user=self.alice, title='问答', type='chat', agent=self.agent)

    @staticmethod
    def _read_stream(resp):
        return b''.join(resp.streaming_content).decode('utf-8')

    @staticmethod
    def _fake_stream(events, capture):
        def gen(agent_id, history, message, **kwargs):
            capture.update(kwargs)
            for ev in events:
                if callable(ev):
                    ev(capture)
                    continue
                yield ev
        return gen

    @staticmethod
    def _request_event():
        return {
            'type': 'permission_request', 'request_id': 'req-1',
            'call_id': 'call-1', 'tool_name': 'list_tickets',
            'source_name': '工单系统', 'display': '工单系统·list_tickets',
            'args': '{"status": "open"}', 'timeout_seconds': 60,
            'can_always': True,
        }

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_stream_forwards_permission_events_and_records_meta(self, _health):
        capture = {}

        def remember(c):
            # 正文落库前写入「本会话始终允许」，验证收尾不会用旧快照覆盖授权
            c['permission_session']._on_grant('mcp__ticket__list_tickets')
            c['permission_session']._on_grant('mcp__ticket__list_tickets')

        events = [
            self._request_event(),
            {'type': 'keepalive'},
            {'type': 'permission_result', 'request_id': 'req-1',
             'call_id': 'call-1', 'display': '工单系统·list_tickets',
             'decision': 'allow_once'},
            remember,
            {'type': 'chunk', 'content': '已批准执行'},
        ]
        with patch('backend.apps.chat.views.run_profile_stream',
                   side_effect=self._fake_stream(events, capture)):
            resp = self.client.post('/api/chat/chat/send/', {
                'message': '查工单', 'conversation_id': self.conv.id, 'stream': True,
            }, format='json')
            body = self._read_stream(resp)

        self.assertIn('event: permission_request', body)
        self.assertIn(': keep-alive', body)
        self.assertIn('event: permission_result', body)
        answer = Message.objects.get(role='assistant')
        self.assertEqual(answer.content, '已批准执行')
        self.assertEqual(answer.meta['permission'], [
            {'tool': '工单系统·list_tickets', 'decision': 'allow_once'}])
        # allow_once 本身不写会话档；这里验证的是写入接线与同工具去重
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.tool_grants, ['mcp__ticket__list_tickets'])

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_disconnect_during_permission_persists_interrupted_message(self, _health):
        capture = {}
        with patch('backend.apps.chat.views.run_profile_stream',
                   side_effect=self._fake_stream([self._request_event()], capture)):
            resp = self.client.post('/api/chat/chat/send/', {
                'message': '查工单', 'conversation_id': self.conv.id, 'stream': True,
            }, format='json')
            for chunk in resp.streaming_content:
                if b'permission_request' in chunk:
                    break
            resp.close()

        answer = Message.objects.filter(role='assistant').last()
        self.assertIsNotNone(answer)
        self.assertEqual(answer.content, '')
        self.assertTrue(answer.meta['stopped'])
        self.assertTrue(answer.meta['permission_interrupted'])

    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_trial_forwards_permission_events_without_persisting(self, _health):
        capture = {}
        events = [
            self._request_event(),
            {'type': 'permission_result', 'request_id': 'req-1',
             'call_id': 'call-1', 'display': '工单系统·list_tickets',
             'decision': 'allow_session'},
            {'type': 'chunk', 'content': '已处理'},
        ]
        with patch('backend.apps.chat.views.run_profile_stream',
                   side_effect=self._fake_stream(events, capture)):
            resp = self.client.post('/api/chat/chat/trial/', {
                'agent_id': self.agent.pk, 'message': '查工单',
            }, format='json')
            body = self._read_stream(resp)

        self.assertIn('event: permission_request', body)
        self.assertIn('event: permission_result', body)
        # 试聊不落会话档：on_grant 为空
        self.assertIsNone(capture['permission_session']._on_grant)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Conversation.objects.count(), 1)

    def test_permission_endpoint_status_codes(self):
        from backend.apps.chat import permissions

        def record(rid, *, status='pending', expires=60, user_id=None,
                   instance_id=None):
            resolved = status != 'pending'
            return PendingPermission.objects.create(
                request_id=rid,
                user_id=user_id or self.alice.id,
                conversation_id=self.conv.id,
                agent_id=self.agent.pk,
                call_id='call-1',
                tool_name='list_tickets',
                display='工单系统·list_tickets',
                status='resolved' if resolved else 'pending',
                decision=status if resolved else '',
                instance_id=instance_id or permissions.INSTANCE_ID,
                expires_at=timezone.now() + timedelta(seconds=expires),
            )

        url = '/api/chat/chat/permission/'
        resp = self.client.post(url, {'request_id': '', 'decision': 'yes'},
                                format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_permission_request')

        resp = self.client.post(url, {'request_id': 'nope', 'decision': 'deny'},
                                format='json')
        self.assertEqual(resp.status_code, 404)

        record('expired', expires=-1)
        resp = self.client.post(url, {'request_id': 'expired',
                                      'decision': 'deny'}, format='json')
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.data['code'], 'permission_expired')

        record('done', status='allow_once')
        resp = self.client.post(url, {'request_id': 'done',
                                      'decision': 'deny'}, format='json')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'permission_already_resolved')

        record('old-boot', instance_id='previous-boot')
        resp = self.client.post(url, {'request_id': 'old-boot',
                                      'decision': 'allow_once'}, format='json')
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.data['code'], 'permission_restarted')

        record('pending')
        resp = self.client.post(url, {'request_id': 'pending',
                                      'decision': 'allow_once'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['code'], 'ok')
        self.assertEqual(
            PendingPermission.objects.get(request_id='pending').decision,
            'allow_once')


class CrossSessionPreferenceTests(TestCase):
    """D8：跨会话偏好的内容边界、隔离、上限、注入与开关"""

    def setUp(self):
        self.alice = _make_user('alice')
        self.bob = _make_user('bob')
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)
        self.agent = _make_agent('工单助手', self.alice,
                                 default_preferences=['先给结论再给依据'])
        self.conv = Conversation.objects.create(
            user=self.alice, title='问答', type='chat', agent=self.agent)
        Message.objects.create(conversation=self.conv, role='user', content='怎么报销')
        Message.objects.create(conversation=self.conv, role='assistant',
                               content='先登录系统再提交')

    def _create(self, content, agent_id=None):
        return self.client.post('/api/chat/preferences/', {
            'agent_id': agent_id or self.agent.id, 'content': content}, format='json')

    def test_create_and_list_shows_unpersisted_preset(self):
        resp = self._create('回答先给结论')
        self.assertEqual(resp.status_code, 201, resp.content)
        groups = self.client.get('/api/chat/preferences/').data['groups']
        self.assertEqual(len(groups), 1)
        items = groups[0]['items']
        self.assertEqual([i['content'] for i in items],
                         ['先给结论再给依据', '回答先给结论'])
        preset = items[0]
        self.assertIsNone(preset['id'])          # 没有行记录的预置项也能列出
        self.assertFalse(preset['muted'])
        self.assertEqual(items[1]['source'], 'user')

    def test_rejects_unsafe_or_empty_content(self):
        for bad in ['', '   ', '见 https://example.com', '工号 202601']:
            with self.subTest(bad=bad):
                self.assertEqual(self._create(bad).status_code, 400, bad)

    def test_limit_counts_active_presets(self):
        # 预置 1 条，最多还能加 7 条自建
        for i in range(7):
            self.assertEqual(self._create(f'偏好{i}').status_code, 201, i)
        resp = self._create('第九条')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('最多', str(resp.data))

    def test_isolation_between_users(self):
        rid = self._create('只属于 alice').data['id']
        bob_client = APIClient()
        bob_client.force_authenticate(user=self.bob)
        groups = bob_client.get('/api/chat/preferences/').data['groups']
        # 预置项对所有使用者可见（便于逐条关闭），但看不到 alice 自建的条目
        contents = [i['content'] for g in groups for i in g['items']]
        self.assertEqual(contents, ['先给结论再给依据'])
        self.assertEqual(
            bob_client.delete(f'/api/chat/preferences/{rid}/').status_code, 404)

    def test_delete_user_item_and_mute_restore_preset(self):
        rid = self._create('临时偏好').data['id']
        self.assertTrue(
            self.client.delete(f'/api/chat/preferences/{rid}/').data['deleted'])

        # 预置项：DELETE 是静音而非删除，PATCH 可恢复
        pid = self.client.post('/api/chat/preferences/preset/', {
            'agent_id': self.agent.id, 'content': '先给结论再给依据',
            'muted': False}, format='json').data['id']
        resp = self.client.delete(f'/api/chat/preferences/{pid}/')
        self.assertEqual(resp.data, {'muted': True, 'deleted': False})
        self.assertTrue(UserPreference.objects.get(pk=pid).muted)
        self.assertFalse(self.client.patch(
            f'/api/chat/preferences/{pid}/', {'muted': False},
            format='json').data['muted'])

    def test_muted_preset_not_injected(self):
        resp = self.client.post('/api/chat/preferences/preset/', {
            'agent_id': self.agent.id, 'content': '先给结论再给依据',
            'muted': True}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(UserPreference.objects.filter(
            user=self.alice, agent=self.agent, muted=True).exists())
        texts, _ = active_preference_texts(self.alice, self.agent)
        self.assertNotIn('先给结论再给依据', texts)

    @patch('backend.apps.chat.views.run_profile_reply', return_value=('好的', [], {'input_tokens': 0, 'output_tokens': 0, 'cache_input_tokens': 0, 'cache_creation_input_tokens': 0, 'model_calls': 0}))
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_preferences_injected_in_prompt_and_counted(self, _health, reply):
        self._create('用「你」称呼我')
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '再说一次', 'conversation_id': self.conv.id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

        prompt = reply.call_args.kwargs['preference_prompt']
        self.assertIn('【用户偏好】', prompt)
        self.assertIn('先给结论再给依据', prompt)
        self.assertIn('用「你」称呼我', prompt)
        # 预置在前、用户自建在后
        self.assertLess(prompt.index('先给结论再给依据'), prompt.index('用「你」称呼我'))
        # 属于用户输入、不改变授权
        self.assertIn('不改变你的工具授权', prompt)

        self.assertEqual(resp.data['preferences'], 2)
        answer = self.conv.messages.filter(role='assistant').latest('created_at')
        self.assertEqual(answer.meta.get('preferences'), 2)
        self.assertIsNotNone(
            UserPreference.objects.get(content='用「你」称呼我').last_used_at)

    @patch('backend.apps.chat.views.run_profile_reply', return_value=('好的', [], {'input_tokens': 0, 'output_tokens': 0, 'cache_input_tokens': 0, 'cache_creation_input_tokens': 0, 'model_calls': 0}))
    @patch('backend.apps.chat.views.check_health', return_value={'status': 'ok'})
    def test_conversation_switch_off_stops_injection(self, _health, reply):
        self._create('用「你」称呼我')
        Conversation.objects.filter(pk=self.conv.pk).update(use_preferences=False)
        resp = self.client.post('/api/chat/chat/send/', {
            'message': '再说一次', 'conversation_id': self.conv.id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(reply.call_args.kwargs['preference_prompt'], '')
        self.assertEqual(resp.data['preferences'], 0)
        answer = self.conv.messages.filter(role='assistant').latest('created_at')
        self.assertNotIn('preferences', answer.meta or {})

    @patch('backend.apps.common.llm.chat_completion',
           side_effect=RuntimeError('模型挂了'))
    def test_suggest_falls_back_when_model_unavailable(self, _llm):
        resp = self.client.post('/api/chat/preferences/suggest/', {
            'conversation_id': self.conv.id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['suggestion'], '回答时保持与本次相同的格式与口径')

    def test_suggest_rejects_others_conversation(self):
        bob_client = APIClient()
        bob_client.force_authenticate(user=self.bob)
        resp = bob_client.post('/api/chat/preferences/suggest/', {
            'conversation_id': self.conv.id}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_suggest_requires_bound_agent(self):
        plain = Conversation.objects.create(user=self.alice, title='通用', type='chat')
        resp = self.client.post('/api/chat/preferences/suggest/', {
            'conversation_id': plain.id}, format='json')
        self.assertEqual(resp.status_code, 400)
