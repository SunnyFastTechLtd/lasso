import base64
import os
import socket
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from docx import Document as DocxDocument
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches
from rest_framework.test import APIClient, APIRequestFactory

from backend.apps.knowledge.models import (
    KnowledgeBase, Document, DocumentChunk, TiangongRun, TiangongSource,
    TiangongSourceProposal, TiangongSearchBinding,
)
from backend.apps.knowledge import discovery, searxng, tiangong, mcp_search, bing
from backend.apps.mcp.models import MCPServer, MCPTool
from backend.apps.settings.models import SystemSetting
from backend.apps.knowledge.serializers import DocumentSerializer, KnowledgeBaseSerializer
from backend.apps.knowledge.services import (
    clean_text, split_text, extract_text, extract_pages, process_document, search_chunks,
)
from backend.apps.knowledge.ocr import (
    OcrOutcome, ocr_image_bytes, tesseract_status,
)
from backend.apps.knowledge.tasks import process_document_task, tiangong_run_task
from backend.apps.common.embedding import embed_texts, embed_query
from backend.apps.users.models import AuditLog, User


class SplitTextTests(SimpleTestCase):
    """文本分块：定长 + 重叠，有序覆盖全文"""

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(split_text(''), [])

    def test_short_text_returns_single_chunk(self):
        self.assertEqual(split_text('你好世界', chunk_size=100, overlap=10), ['你好世界'])

    def test_long_text_split_with_overlap_in_order(self):
        text = '一二三四五六七八九十' * 10  # 100 字符
        chunks = split_text(text, chunk_size=30, overlap=6)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 30 for c in chunks))
        # 相邻块保持 6 字符重叠
        for prev, nxt in zip(chunks, chunks[1:]):
            self.assertEqual(prev[-6:], nxt[:6])
        # 去除重叠后按序拼接可还原全文
        rebuilt = chunks[0] + ''.join(c[6:] for c in chunks[1:])
        self.assertEqual(rebuilt, text)


class ExtractTextTests(SimpleTestCase):
    """文本提取：按文件类型分发，txt/md 直读，pdf 走 pypdf，Office 走专用解析库"""

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix='.txt')
        os.close(fd)
        self._extra_tmps = []

    def tearDown(self):
        os.unlink(self.tmp_path)
        for path in self._extra_tmps:
            if os.path.exists(path):
                os.unlink(path)

    def _make_tmp(self, suffix: str) -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        self._extra_tmps.append(path)
        return path

    def test_txt_file_read_as_utf8(self):
        with open(self.tmp_path, 'w', encoding='utf-8') as f:
            f.write('一体机产品手册\n第一章 产品概述')
        self.assertEqual(
            extract_text(self.tmp_path, 'txt'),
            '一体机产品手册\n第一章 产品概述',
        )

    @patch('backend.apps.knowledge.services.PdfReader')
    def test_pdf_pages_joined_with_newline(self, mock_reader_cls):
        pages = []
        for text in ('第一页内容', '第二页内容'):
            page = MagicMock()
            page.extract_text.return_value = text
            pages.append(page)
        mock_reader_cls.return_value.pages = pages

        self.assertEqual(
            extract_text('dummy.pdf', 'pdf'),
            '第一页内容\n第二页内容',
        )

    def test_docx_paragraphs_and_tables_extracted(self):
        path = self._make_tmp('.docx')
        doc = DocxDocument()
        doc.add_paragraph('第一章 产品概述')
        doc.add_paragraph('')  # 空段落应跳过
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = '型号'
        table.cell(0, 1).text = 'AI 一体机'
        doc.save(path)

        self.assertEqual(
            extract_text(path, 'docx'),
            '第一章 产品概述\n型号 | AI 一体机',
        )

    def test_xlsx_sheets_extracted_with_headers(self):
        path = self._make_tmp('.xlsx')
        wb = Workbook()
        ws1 = wb.active
        ws1.title = '价格表'
        ws1.append(['产品', '单价'])
        ws1.append(['一体机', 19999])
        ws1.append([None, None])  # 空行应跳过
        ws2 = wb.create_sheet('库存')
        ws2.append(['仓库', '数量'])
        wb.save(path)

        self.assertEqual(
            extract_text(path, 'xlsx'),
            '## 价格表\n产品\t单价\n一体机\t19999\n## 库存\n仓库\t数量',
        )

    def test_pptx_slides_extracted_with_page_headers(self):
        path = self._make_tmp('.pptx')
        prs = Presentation()
        slide1 = prs.slides.add_slide(prs.slide_layouts[6])  # 空白版式
        box1 = slide1.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box1.text_frame.text = '产品发布会'
        slide2 = prs.slides.add_slide(prs.slide_layouts[6])
        box2 = slide2.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box2.text_frame.text = '核心亮点'
        prs.save(path)

        self.assertEqual(
            extract_text(path, 'pptx'),
            '## 第 1 页\n产品发布会\n## 第 2 页\n核心亮点',
        )

    def test_unsupported_type_raises(self):
        with self.assertRaises(ValueError):
            extract_text('dummy.epub', 'epub')


class ProcessDocumentTests(TestCase):
    """文档处理流水线：提取 → 分块 → 向量化 → 落库"""

    def setUp(self):
        self.media_dir = tempfile.mkdtemp()
        self.kb = KnowledgeBase.objects.create(name='测试库')

    def _make_doc(self, content: str = None, file_name: str = 'test.txt') -> Document:
        if content is not None:
            with open(os.path.join(self.media_dir, file_name), 'w', encoding='utf-8') as f:
                f.write(content)
        return Document.objects.create(
            knowledge_base=self.kb,
            title='测试文档',
            file_name=file_name,
            file_type='txt',
            file_path=file_name,
            status='pending',
        )

    @patch('backend.apps.knowledge.services.embeddings')
    def test_success_creates_chunks_and_updates_counts(self, mock_embeddings):
        content = '第一段内容。' * 100  # 600 字符，默认 500/50 切 2 块
        mock_embeddings.return_value = [[1.0, 0.0], [0.0, 1.0]]

        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc(content)
            process_document(doc)

        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed')
        self.assertEqual(doc.chunk_count, 2)

        chunks = list(doc.chunks.all())
        self.assertEqual(len(chunks), 2)
        self.assertEqual([c.chunk_index for c in chunks], [0, 1])
        self.assertEqual(chunks[0].content, content[:500])
        self.assertEqual(chunks[1].content, content[450:])
        self.assertEqual(chunks[0].embedding, [1.0, 0.0])
        self.assertEqual(chunks[1].embedding, [0.0, 1.0])

        self.kb.refresh_from_db()
        self.assertEqual(self.kb.chunk_count, 2)

    @patch('backend.apps.knowledge.services.embeddings')
    def test_extract_failure_marks_failed_without_chunks(self, mock_embeddings):
        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc(content=None, file_name='missing.txt')
            process_document(doc)

        doc.refresh_from_db()
        self.assertEqual(doc.status, 'failed')
        self.assertTrue(doc.error_msg)
        self.assertEqual(doc.chunk_count, 0)
        self.assertEqual(DocumentChunk.objects.filter(document=doc).count(), 0)
        mock_embeddings.assert_not_called()

        self.kb.refresh_from_db()
        self.assertEqual(self.kb.chunk_count, 0)


class DocumentUploadAPITests(TestCase):
    """上传接口：multipart 上传真实文件后应走真实处理流水线（分块+向量化落库）"""

    def setUp(self):
        self.media_dir = tempfile.mkdtemp()
        self.user = User.objects.create_user(username='uploader', password='demo-pass-9x2')
        self.kb = KnowledgeBase.objects.create(name='上传测试库', created_by=self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch('backend.apps.knowledge.services.embeddings')
    def test_upload_txt_triggers_real_pipeline(self, mock_embeddings):
        content = '人工智能正在改变世界。' * 100  # 1100 字符 → 500/50 切 3 块
        mock_embeddings.return_value = [[1.0], [2.0], [3.0]]
        upload = SimpleUploadedFile('notes.txt', content.encode('utf-8'), content_type='text/plain')

        with override_settings(MEDIA_ROOT=self.media_dir):
            resp = self.client.post('/api/knowledge/documents/upload/', {
                'knowledge_base_id': self.kb.id,
                'title': 'AI 笔记',
                'file': upload,
            }, format='multipart')

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.data['file_url'])
        doc = Document.objects.get(title='AI 笔记')
        self.assertEqual(doc.status, 'completed')
        self.assertEqual(doc.chunk_count, 3)

        chunks = list(doc.chunks.all())
        self.assertEqual([c.chunk_index for c in chunks], [0, 1, 2])
        self.assertEqual(chunks[0].content, content[:500])
        self.assertEqual(chunks[0].embedding, [1.0])

        self.kb.refresh_from_db()
        self.assertEqual(self.kb.chunk_count, 3)


class DocumentSerializerTests(TestCase):
    """文档序列化器：file_url 提供原件下载地址"""

    def setUp(self):
        self.kb = KnowledgeBase.objects.create(name='序列化库')

    def _make_doc(self, file_path='knowledge/2026/01/m.txt') -> Document:
        return Document.objects.create(
            knowledge_base=self.kb, title='手册', file_name='m.txt',
            file_type='txt', file_size=10, file_path=file_path, status='completed',
        )

    def test_file_url_absolute_with_request(self):
        doc = self._make_doc()
        request = APIRequestFactory().get('/api/knowledge/documents/')
        data = DocumentSerializer(doc, context={'request': request}).data
        self.assertTrue(data['file_url'].startswith('http'))
        self.assertTrue(data['file_url'].endswith('/media/knowledge/2026/01/m.txt'))

    def test_file_url_relative_without_request(self):
        doc = self._make_doc()
        data = DocumentSerializer(doc).data
        self.assertTrue(data['file_url'].endswith('media/knowledge/2026/01/m.txt'))

    def test_file_url_none_without_file(self):
        doc = self._make_doc(file_path='')
        data = DocumentSerializer(doc).data
        self.assertIsNone(data['file_url'])


class SearchChunksTests(TestCase):
    """向量语义检索：余弦相似度降序、top_k 截断、限定知识库范围"""

    def setUp(self):
        self.kb = KnowledgeBase.objects.create(name='检索库')
        self.doc = Document.objects.create(
            knowledge_base=self.kb, title='AI 手册', file_name='ai.txt',
            file_type='txt', file_size=100, file_path='documents/ai.txt',
            status='completed', chunk_count=3,
        )
        DocumentChunk.objects.create(
            document=self.doc, chunk_index=0, content='机器学习基础章节', embedding=[1.0, 0.0],
        )
        DocumentChunk.objects.create(
            document=self.doc, chunk_index=1, content='深度学习进阶章节', embedding=[0.9, 0.1],
        )
        DocumentChunk.objects.create(
            document=self.doc, chunk_index=2, content='烹饪技巧章节', embedding=[0.0, 1.0],
        )

    def test_empty_base_returns_empty_list(self):
        other_kb = KnowledgeBase.objects.create(name='空库')
        self.assertEqual(search_chunks(other_kb.id, [1.0, 0.0]), [])

    def test_ranked_by_cosine_similarity_with_top_k(self):
        results = search_chunks(self.kb.id, [1.0, 0.0], top_k=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['content'], '机器学习基础章节')
        self.assertAlmostEqual(results[0]['score'], 1.0, places=5)
        self.assertEqual(results[1]['content'], '深度学习进阶章节')
        self.assertGreater(results[1]['score'], 0.99)
        self.assertLess(results[1]['score'], 1.0)
        self.assertEqual(results[0]['doc_title'], 'AI 手册')
        self.assertEqual(results[0]['file_name'], 'ai.txt')
        self.assertIn('chunk_index', results[0])

    def test_scoped_to_given_knowledge_base(self):
        other_kb = KnowledgeBase.objects.create(name='别的库')
        other_doc = Document.objects.create(
            knowledge_base=other_kb, title='其他文档', file_name='other.txt',
            file_type='txt', file_size=10, file_path='documents/other.txt',
            status='completed', chunk_count=1,
        )
        DocumentChunk.objects.create(
            document=other_doc, chunk_index=0, content='其他库内容', embedding=[1.0, 0.0],
        )
        results = search_chunks(self.kb.id, [1.0, 0.0], top_k=10)
        self.assertEqual(len(results), 3)
        self.assertNotIn('其他库内容', [r['content'] for r in results])

    def test_skips_chunks_without_embedding(self):
        DocumentChunk.objects.create(
            document=self.doc, chunk_index=3, content='未向量化分块', embedding=[],
        )
        results = search_chunks(self.kb.id, [1.0, 0.0], top_k=10)
        self.assertNotIn('未向量化分块', [r['content'] for r in results])

    def test_results_include_chunk_meta(self):
        DocumentChunk.objects.filter(document=self.doc, chunk_index=0).update(
            meta={'page': 3, 'ocr': True, 'ocr_engine': 'tesseract'})
        results = search_chunks(self.kb.id, [1.0, 0.0], top_k=1)
        self.assertEqual(results[0]['meta'],
                         {'page': 3, 'ocr': True, 'ocr_engine': 'tesseract'})


class ChatAskSourcesTests(TestCase):
    """知识库问答接口：sources 应来自真实语义检索结果，且带引用定位字段"""

    def setUp(self):
        self.user = User.objects.create_user(username='asker', password='demo-pass-9x2')
        self.kb = KnowledgeBase.objects.create(name='问答库')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch('backend.apps.knowledge.views.run_agent')
    @patch('backend.apps.knowledge.views.create_agent_blocking')
    @patch('backend.apps.knowledge.views.search_chunks')
    @patch('backend.apps.knowledge.views.single_embedding')
    def test_sources_come_from_real_semantic_search(
        self, mock_embed, mock_search, mock_create, mock_run,
    ):
        mock_embed.return_value = [1.0, 0.0]
        mock_search.return_value = [
            {'chunk_id': 1, 'chunk_index': 0, 'content': '片段一', 'score': 0.97654,
             'doc_id': 11, 'doc_title': 'AI 手册', 'file_name': 'ai.txt',
             'meta': {'page': 3, 'ocr': True, 'ocr_engine': 'tesseract'}},
            {'chunk_id': 2, 'chunk_index': 1, 'content': '片段二', 'score': 0.81234,
             'doc_id': 11, 'doc_title': 'AI 手册', 'file_name': 'ai.txt'},
        ]
        mock_create.return_value = object()
        mock_run.return_value = '答案'

        resp = self.client.post('/api/knowledge/chat/ask/', {
            'question': '什么是机器学习',
            'knowledge_base_id': self.kb.id,
        }, format='json')

        self.assertEqual(resp.status_code, 200, resp.content)
        mock_embed.assert_called_once_with('什么是机器学习')
        mock_search.assert_called_once_with(self.kb.id, [1.0, 0.0], top_k=3)
        self.assertEqual(resp.data['sources'], [
            {'title': 'AI 手册', 'score': 0.98, 'chunk_id': 1, 'doc_id': 11,
             'chunk_index': 0, 'content': '片段一',
             'meta': {'page': 3, 'ocr': True, 'ocr_engine': 'tesseract'}},
            {'title': 'AI 手册', 'score': 0.81, 'chunk_id': 2, 'doc_id': 11,
             'chunk_index': 1, 'content': '片段二', 'meta': {}},
        ])


class ChunkInactiveSearchTests(TestCase):
    """禁检分块（is_active=False）不参与本地检索"""

    def setUp(self):
        self.kb = KnowledgeBase.objects.create(name='禁检库')
        self.doc = Document.objects.create(
            knowledge_base=self.kb, title='手册', file_name='a.txt',
            file_type='txt', file_size=10, file_path='documents/a.txt',
            status='completed', chunk_count=2,
        )

    def test_local_search_excludes_inactive_chunks(self):
        DocumentChunk.objects.create(
            document=self.doc, chunk_index=0, content='可检索内容', embedding=[1.0, 0.0],
        )
        DocumentChunk.objects.create(
            document=self.doc, chunk_index=1, content='禁检内容',
            embedding=[1.0, 0.0], is_active=False,
        )
        results = search_chunks(self.kb.id, [1.0, 0.0], top_k=10)
        contents = [r['content'] for r in results]
        self.assertIn('可检索内容', contents)
        self.assertNotIn('禁检内容', contents)

    def test_search_results_include_doc_id(self):
        DocumentChunk.objects.create(
            document=self.doc, chunk_index=0, content='内容', embedding=[1.0, 0.0],
        )
        results = search_chunks(self.kb.id, [1.0, 0.0], top_k=10)
        self.assertEqual(results[0]['doc_id'], self.doc.id)


class DocumentChunksAPITests(TestCase):
    """分块预览 + 禁检切换接口"""

    def setUp(self):
        self.user = User.objects.create_user(username='chunker', password='demo-pass-9x2')
        self.kb = KnowledgeBase.objects.create(name='分块库', created_by=self.user)
        self.doc = Document.objects.create(
            knowledge_base=self.kb, title='手册', file_name='a.txt',
            file_type='txt', file_size=10, file_path='documents/a.txt',
            status='completed', chunk_count=2,
        )
        self.c0 = DocumentChunk.objects.create(
            document=self.doc, chunk_index=0, content='第一段', embedding=[0.1, 0.2],
        )
        self.c1 = DocumentChunk.objects.create(
            document=self.doc, chunk_index=1, content='第二段', embedding=[],
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_chunks_list_ordered_with_vector_status(self):
        resp = self.client.get(f'/api/knowledge/documents/{self.doc.id}/chunks/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data), 2)
        self.assertEqual(resp.data[0]['chunk_index'], 0)
        self.assertEqual(resp.data[0]['content'], '第一段')
        self.assertTrue(resp.data[0]['has_embedding'])
        self.assertTrue(resp.data[0]['is_active'])
        self.assertFalse(resp.data[1]['has_embedding'])

    def test_chunk_meta_exposed_for_page_and_ocr_badges(self):
        self.c0.meta = {'page': 2, 'ocr': True, 'ocr_engine': 'llm'}
        self.c0.save(update_fields=['meta'])
        resp = self.client.get(f'/api/knowledge/documents/{self.doc.id}/chunks/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data[0]['meta'],
                         {'page': 2, 'ocr': True, 'ocr_engine': 'llm'})

    def test_chunks_list_unauthenticated_401(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(f'/api/knowledge/documents/{self.doc.id}/chunks/')
        self.assertEqual(resp.status_code, 401)

    def test_toggle_disable_and_enable(self):
        url = f'/api/knowledge/documents/{self.doc.id}/chunks/{self.c0.id}/'
        resp = self.client.patch(url, {'is_active': False}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data['is_active'])
        self.c0.refresh_from_db()
        self.assertFalse(self.c0.is_active)

        resp = self.client.patch(url, {'is_active': True}, format='json')
        self.assertTrue(resp.data['is_active'])

    def test_toggle_same_state_is_noop(self):
        url = f'/api/knowledge/documents/{self.doc.id}/chunks/{self.c0.id}/'
        resp = self.client.patch(url, {'is_active': True}, format='json')
        self.assertEqual(resp.status_code, 200)

    def test_toggle_rejects_non_bool(self):
        url = f'/api/knowledge/documents/{self.doc.id}/chunks/{self.c0.id}/'
        resp = self.client.patch(url, {'is_active': 'yes'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_toggle_404_for_wrong_chunk_or_doc(self):
        url = f'/api/knowledge/documents/{self.doc.id}/chunks/9999/'
        self.assertEqual(self.client.patch(url, {'is_active': False}, format='json').status_code, 404)
        url = f'/api/knowledge/documents/9999/chunks/{self.c0.id}/'
        self.assertEqual(self.client.patch(url, {'is_active': False}, format='json').status_code, 404)

    @patch('backend.apps.knowledge.views.vectorstore')
    def test_toggle_syncs_qdrant_on_disable_and_enable(self, mock_vs):
        mock_vs.is_enabled.return_value = True
        url = f'/api/knowledge/documents/{self.doc.id}/chunks/{self.c0.id}/'

        self.client.patch(url, {'is_active': False}, format='json')
        mock_vs.delete_points.assert_called_once_with(self.kb.id, [self.c0.id])

        self.client.patch(url, {'is_active': True}, format='json')
        mock_vs.upsert_chunks.assert_called_once_with(
            self.kb.id, [(self.c0.id, [0.1, 0.2], self.doc.id)],
        )

    @patch('backend.apps.knowledge.views.vectorstore')
    def test_enable_without_embedding_skips_upsert(self, mock_vs):
        mock_vs.is_enabled.return_value = True
        url = f'/api/knowledge/documents/{self.doc.id}/chunks/{self.c1.id}/'
        self.client.patch(url, {'is_active': False}, format='json')
        mock_vs.reset_mock()
        mock_vs.is_enabled.return_value = True
        self.client.patch(url, {'is_active': True}, format='json')
        mock_vs.upsert_chunks.assert_not_called()


class EmbeddingBackendTests(SimpleTestCase):
    """本地嵌入（fastembed）：passage/query 双入口"""

    @patch('backend.apps.common.embedding._get_local_model')
    def test_texts_uses_passage_embed(self, mock_get_model):
        model = MagicMock()
        model.passage_embed.return_value = iter([[0.1, 0.2]])
        mock_get_model.return_value = model

        self.assertEqual(embed_texts(['你好']), [[0.1, 0.2]])
        model.passage_embed.assert_called_once_with(['你好'])

    @patch('backend.apps.common.embedding._get_local_model')
    def test_query_uses_query_embed(self, mock_get_model):
        model = MagicMock()
        model.query_embed.return_value = iter([[0.3, 0.4]])
        mock_get_model.return_value = model

        self.assertEqual(embed_query('问题'), [0.3, 0.4])
        model.query_embed.assert_called_once_with(['问题'])

    def test_texts_empty_returns_empty(self):
        self.assertEqual(embed_texts([]), [])


class ProcessDocumentTaskTests(TestCase):
    """知识库 Celery 任务：调用流水线 / 文档缺失时忽略"""

    def setUp(self):
        self.kb = KnowledgeBase.objects.create(name='任务库')

    @patch('backend.apps.knowledge.tasks.process_document')
    def test_task_calls_process_document(self, mock_process):
        doc = Document.objects.create(
            knowledge_base=self.kb, title='待处理', file_name='a.txt',
            file_type='txt', file_size=10, file_path='knowledge/a.txt', status='pending',
        )
        process_document_task(doc.id)
        mock_process.assert_called_once()
        self.assertEqual(mock_process.call_args[0][0].id, doc.id)

    @patch('backend.apps.knowledge.tasks.process_document')
    def test_task_ignores_missing_document(self, mock_process):
        process_document_task(999999)
        mock_process.assert_not_called()


class UploadTaskDispatchTests(TestCase):
    """上传接口：向量化经 Celery 任务投递（eager 模式下同步完成）"""

    def setUp(self):
        self.media_dir = tempfile.mkdtemp()
        self.user = User.objects.create_user(username='task_uploader', password='demo-pass-9x2')
        self.kb = KnowledgeBase.objects.create(name='任务派发库', created_by=self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch('backend.apps.knowledge.views.process_document_task')
    def test_upload_enqueues_celery_task(self, mock_task):
        upload = SimpleUploadedFile('doc.txt', '内容'.encode('utf-8'), content_type='text/plain')
        with override_settings(MEDIA_ROOT=self.media_dir):
            resp = self.client.post('/api/knowledge/documents/upload/', {
                'knowledge_base_id': self.kb.id,
                'file': upload,
            }, format='multipart')

        self.assertEqual(resp.status_code, 201, resp.content)
        doc = Document.objects.latest('id')
        mock_task.delay.assert_called_once_with(doc.id)


class QdrantProcessDocumentTests(TestCase):
    """Qdrant 向量库接入：启用时写入向量，禁用时跳过"""

    def setUp(self):
        self.media_dir = tempfile.mkdtemp()
        self.kb = KnowledgeBase.objects.create(name='向量库')

    def _make_doc(self, content: str = '短内容') -> Document:
        file_name = 'vdoc.txt'
        with open(os.path.join(self.media_dir, file_name), 'w', encoding='utf-8') as f:
            f.write(content)
        return Document.objects.create(
            knowledge_base=self.kb, title='向量文档', file_name=file_name,
            file_type='txt', file_size=len(content), file_path=file_name, status='pending',
        )

    @patch('backend.apps.knowledge.services.vectorstore')
    @patch('backend.apps.knowledge.services.embeddings')
    def test_upserts_to_qdrant_when_enabled(self, mock_embeddings, mock_vs):
        mock_embeddings.return_value = [[1.0, 0.0]]
        mock_vs.is_enabled.return_value = True

        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc()
            process_document(doc)

        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed')
        mock_vs.delete_document_vectors.assert_called_once_with(self.kb.id, doc.id)
        mock_vs.upsert_chunks.assert_called_once()
        kb_id, points = mock_vs.upsert_chunks.call_args[0]
        self.assertEqual(kb_id, self.kb.id)
        chunk = doc.chunks.get()
        self.assertEqual(points, [(chunk.id, [1.0, 0.0], doc.id)])

    @patch('backend.apps.knowledge.services.vectorstore')
    @patch('backend.apps.knowledge.services.embeddings')
    def test_skips_qdrant_when_disabled(self, mock_embeddings, mock_vs):
        mock_embeddings.return_value = [[1.0, 0.0]]
        mock_vs.is_enabled.return_value = False

        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc()
            process_document(doc)

        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed')
        mock_vs.upsert_chunks.assert_not_called()
        mock_vs.delete_document_vectors.assert_not_called()

    @patch('backend.apps.knowledge.services.vectorstore')
    @patch('backend.apps.knowledge.services.embeddings')
    def test_rerun_clears_old_chunks(self, mock_embeddings, mock_vs):
        """重跑幂等：旧分块先删除，不重复累积"""
        mock_embeddings.return_value = [[1.0, 0.0]]
        mock_vs.is_enabled.return_value = False

        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc()
            process_document(doc)
            process_document(doc)

        self.assertEqual(doc.chunks.count(), 1)


class QdrantSearchTests(TestCase):
    """Qdrant 检索分支：按 ANN 命中回表组装，孤儿向量跳过"""

    def setUp(self):
        self.kb = KnowledgeBase.objects.create(name='Q检索库')
        self.doc = Document.objects.create(
            knowledge_base=self.kb, title='Q手册', file_name='q.txt',
            file_type='txt', file_size=100, file_path='documents/q.txt',
            status='completed', chunk_count=2,
        )
        self.chunk1 = DocumentChunk.objects.create(
            document=self.doc, chunk_index=0, content='机器学习章节', embedding=[1.0],
        )
        self.chunk2 = DocumentChunk.objects.create(
            document=self.doc, chunk_index=1, content='深度学习章节', embedding=[0.0],
        )

    @patch('backend.apps.knowledge.services.vectorstore')
    def test_qdrant_branch_assembles_results(self, mock_vs):
        mock_vs.is_enabled.return_value = True
        mock_vs.search.return_value = [
            (self.chunk2.id, 0.95),
            (self.chunk1.id, 0.80),
            (999999, 0.70),  # 孤儿向量：DB 中不存在，应跳过
        ]
        mock_vs.build_chunks_map.side_effect = lambda ids: {
            c.id: c for c in DocumentChunk.objects.filter(id__in=ids).select_related('document')
        }

        results = search_chunks(self.kb.id, [1.0], top_k=3)

        mock_vs.search.assert_called_once_with(self.kb.id, [1.0], 3)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['content'], '深度学习章节')
        self.assertEqual(results[0]['score'], 0.95)
        self.assertEqual(results[0]['doc_title'], 'Q手册')
        self.assertEqual(results[0]['file_name'], 'q.txt')
        self.assertEqual(results[1]['content'], '机器学习章节')

    @patch('backend.apps.knowledge.services.vectorstore')
    def test_qdrant_branch_empty_hits(self, mock_vs):
        mock_vs.is_enabled.return_value = True
        mock_vs.search.return_value = []
        self.assertEqual(search_chunks(self.kb.id, [1.0]), [])


class QdrantCleanupAPITests(TestCase):
    """删除文档/知识库时同步清理 Qdrant 向量"""

    def setUp(self):
        self.user = User.objects.create_user(username='cleaner', password='demo-pass-9x2')
        self.kb = KnowledgeBase.objects.create(name='清理库', created_by=self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _make_doc(self) -> Document:
        return Document.objects.create(
            knowledge_base=self.kb, title='待删', file_name='d.txt',
            file_type='txt', file_size=10, file_path='documents/d.txt', status='completed',
        )

    @patch('backend.apps.knowledge.views.vectorstore')
    def test_delete_document_cleans_vectors(self, mock_vs):
        mock_vs.is_enabled.return_value = True
        doc = self._make_doc()
        resp = self.client.delete(f'/api/knowledge/documents/{doc.id}/delete/')
        self.assertEqual(resp.status_code, 200)
        mock_vs.delete_document_vectors.assert_called_once_with(self.kb.id, doc.id)

    @patch('backend.apps.knowledge.views.vectorstore')
    def test_destroy_document_cleans_vectors(self, mock_vs):
        mock_vs.is_enabled.return_value = True
        doc = self._make_doc()
        resp = self.client.delete(f'/api/knowledge/documents/{doc.id}/')
        self.assertEqual(resp.status_code, 204)
        mock_vs.delete_document_vectors.assert_called_once_with(self.kb.id, doc.id)

    @patch('backend.apps.knowledge.views.vectorstore')
    def test_destroy_kb_drops_collection(self, mock_vs):
        mock_vs.is_enabled.return_value = True
        resp = self.client.delete(f'/api/knowledge/bases/{self.kb.id}/')
        self.assertEqual(resp.status_code, 204)
        mock_vs.drop_collection.assert_called_once_with(self.kb.id)

    @patch('backend.apps.knowledge.views.vectorstore')
    def test_cleanup_failure_does_not_block_delete(self, mock_vs):
        """Qdrant 不可用时 DB 删除照常进行"""
        mock_vs.is_enabled.return_value = True
        mock_vs.delete_document_vectors.side_effect = ConnectionError('qdrant down')
        doc = self._make_doc()
        resp = self.client.delete(f'/api/knowledge/documents/{doc.id}/delete/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Document.objects.filter(id=doc.id).exists())

    @patch('backend.apps.knowledge.views.vectorstore')
    def test_destroy_kb_creates_audit_log(self, mock_vs):
        """删除知识库时写入审计日志"""
        mock_vs.is_enabled.return_value = False
        resp = self.client.delete(f'/api/knowledge/bases/{self.kb.id}/')
        self.assertEqual(resp.status_code, 204)
        log = AuditLog.objects.filter(action='kb_delete', target='清理库').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.actor, self.user)


class DocumentStatsSyncTests(TestCase):
    """删除文档/重跑解析时知识库冗余计数（document/chunk/size）同步"""

    def setUp(self):
        self.user = User.objects.create_user(username='stats', password='demo-pass-9x2')
        self.kb = KnowledgeBase.objects.create(
            name='计数库', document_count=1, chunk_count=2, size_bytes=100,
            created_by=self.user,
        )
        self.doc = Document.objects.create(
            knowledge_base=self.kb, title='文档', file_name='d.txt',
            file_type='txt', file_size=100, file_path='documents/d.txt',
            status='completed', chunk_count=2,
        )
        # 与计数一致的真实分块记录（重跑校准时按表实际删除数计算）
        for i in range(2):
            DocumentChunk.objects.create(
                document=self.doc, chunk_index=i, content=f'分块{i}', embedding=[1.0],
            )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _counters(self):
        self.kb.refresh_from_db()
        return (self.kb.document_count, self.kb.chunk_count, self.kb.size_bytes)

    def test_delete_action_syncs_all_counters(self):
        resp = self.client.delete(f'/api/knowledge/documents/{self.doc.id}/delete/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._counters(), (0, 0, 0))

    def test_destroy_route_syncs_all_counters(self):
        resp = self.client.delete(f'/api/knowledge/documents/{self.doc.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self._counters(), (0, 0, 0))

    @patch('backend.apps.knowledge.services.vectorstore')
    @patch('backend.apps.knowledge.services.embeddings')
    def test_reprocess_does_not_double_count_chunks(self, mock_embeddings, mock_vs):
        """重跑解析（reembed）：先清 2 块再加 2 块，kb.chunk_count 保持 2 而非 4"""
        mock_vs.is_enabled.return_value = False
        mock_embeddings.return_value = [[1.0], [2.0]]
        with patch('backend.apps.knowledge.services.extract_text', return_value='文本'), \
             patch('backend.apps.knowledge.services.split_text', return_value=['块一', '块二']):
            process_document(self.doc)

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, 'completed')
        self.assertEqual(self.doc.chunk_count, 2)
        self.kb.refresh_from_db()
        self.assertEqual(self.kb.chunk_count, 2)


class ReembedCommandTests(TestCase):
    """重建索引命令：同步重跑 / 投递 Celery"""

    def setUp(self):
        self.kb = KnowledgeBase.objects.create(name='重建库')
        self.doc1 = Document.objects.create(
            knowledge_base=self.kb, title='文档一', file_name='1.txt',
            file_type='txt', file_size=10, file_path='documents/1.txt', status='completed',
        )
        self.doc2 = Document.objects.create(
            knowledge_base=self.kb, title='文档二', file_name='2.txt',
            file_type='txt', file_size=10, file_path='documents/2.txt', status='failed',
        )

    @patch('backend.apps.knowledge.management.commands.reembed_knowledge.process_document')
    def test_sync_reprocesses_all_documents(self, mock_process):
        def _mark_completed(doc):
            doc.status = 'completed'
            doc.save(update_fields=['status'])
        mock_process.side_effect = _mark_completed

        call_command('reembed_knowledge')
        self.assertEqual(mock_process.call_count, 2)

    @patch('backend.apps.knowledge.management.commands.reembed_knowledge.process_document_task')
    def test_enqueue_dispatches_tasks(self, mock_task):
        call_command('reembed_knowledge', enqueue=True)
        self.assertEqual(mock_task.delay.call_count, 2)

    @patch('backend.apps.knowledge.management.commands.reembed_knowledge.process_document')
    def test_kb_filter(self, mock_process):
        other_kb = KnowledgeBase.objects.create(name='别的库')
        Document.objects.create(
            knowledge_base=other_kb, title='其他', file_name='3.txt',
            file_type='txt', file_size=10, file_path='documents/3.txt', status='completed',
        )
        call_command('reembed_knowledge', kb=self.kb.id)
        self.assertEqual(mock_process.call_count, 2)


class EnsureCollectionTests(SimpleTestCase):
    """ensure_collection：并发创建同一 collection 时 409 冲突视为成功"""

    def setUp(self):
        super().setUp()
        # 本地测试环境未安装 qdrant-client，模块级名字不存在，需 create=True 占位
        for name in ('VectorParams', 'Distance'):
            patcher = patch(f'backend.apps.knowledge.vectorstore.{name}', create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _mock_client(self, dim, exists=False, create_side_effect=None):
        client = MagicMock()
        client.collection_exists.return_value = exists
        info = MagicMock()
        info.config.params.vectors.size = dim
        client.get_collection.return_value = info
        if create_side_effect:
            client.create_collection.side_effect = create_side_effect
        return client

    @patch('backend.apps.knowledge.vectorstore.get_client')
    def test_creates_when_absent(self, mock_get):
        from backend.apps.knowledge import vectorstore
        client = self._mock_client(dim=512)
        mock_get.return_value = client

        self.assertEqual(vectorstore.ensure_collection(1, 512), vectorstore.collection_name(1))
        client.create_collection.assert_called_once()
        client.get_collection.assert_not_called()

    @patch('backend.apps.knowledge.vectorstore.get_client')
    def test_concurrent_create_conflict_tolerated(self, mock_get):
        """check-then-act 竞态：另一任务已建好 collection，复核维度后复用"""
        from backend.apps.knowledge import vectorstore
        name = vectorstore.collection_name(1)
        client = self._mock_client(dim=512, create_side_effect=Exception(
            f"Unexpected Response: 409 (Conflict) "
            f"Collection `{name}` already exists!"))
        mock_get.return_value = client

        self.assertEqual(vectorstore.ensure_collection(1, 512), name)
        client.get_collection.assert_called_once_with(name)

    @patch('backend.apps.knowledge.vectorstore.get_client')
    def test_conflict_with_dim_mismatch_still_raises(self, mock_get):
        from backend.apps.knowledge import vectorstore
        client = self._mock_client(dim=768, create_side_effect=Exception('already exists'))
        mock_get.return_value = client

        with self.assertRaises(ValueError):
            vectorstore.ensure_collection(1, 512)

    @patch('backend.apps.knowledge.vectorstore.get_client')
    def test_other_create_error_propagates(self, mock_get):
        from backend.apps.knowledge import vectorstore
        client = self._mock_client(dim=512, create_side_effect=ConnectionError('qdrant down'))
        mock_get.return_value = client

        with self.assertRaises(ConnectionError):
            vectorstore.ensure_collection(1, 512)


class KnowledgeScopeVisibilityTests(TestCase):
    """知识库可见范围：公开 / 本部门 / 私有 三档可见性矩阵"""

    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', password='demo-pass-9x2', department='技术部')
        self.colleague = User.objects.create_user(
            username='colleague', password='demo-pass-9x2', department='技术部')
        self.outsider = User.objects.create_user(
            username='outsider', password='demo-pass-9x2', department='市场部')
        self.no_dept = User.objects.create_user(username='nodept', password='demo-pass-9x2')
        self.admin = User.objects.create_user(
            username='kbadmin', password='demo-pass-9x2', is_staff=True)

        self.public_kb = KnowledgeBase.objects.create(
            name='公开库', scope='public', created_by=self.owner)
        self.dept_kb = KnowledgeBase.objects.create(
            name='部门库', scope='department', department='技术部', created_by=self.owner)
        self.private_kb = KnowledgeBase.objects.create(
            name='私有库', scope='private', created_by=self.owner)

        self.client = APIClient()

    def _list_ids(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get('/api/knowledge/bases/')
        self.assertEqual(resp.status_code, 200, resp.content)
        return {item['id'] for item in resp.data['results']}

    def test_public_kb_visible_to_anyone(self):
        self.assertIn(self.public_kb.id, self._list_ids(self.outsider))

    def test_department_kb_visible_to_same_department(self):
        self.assertIn(self.dept_kb.id, self._list_ids(self.colleague))

    def test_department_kb_hidden_from_other_department(self):
        self.assertNotIn(self.dept_kb.id, self._list_ids(self.outsider))

    def test_department_kb_hidden_from_user_without_department(self):
        # department 为空的用户不参与部门共享（避免空串互相可见）
        self.assertNotIn(self.dept_kb.id, self._list_ids(self.no_dept))

    def test_private_kb_visible_only_to_creator(self):
        self.assertIn(self.private_kb.id, self._list_ids(self.owner))
        self.assertNotIn(self.private_kb.id, self._list_ids(self.colleague))

    def test_admin_sees_everything(self):
        ids = self._list_ids(self.admin)
        self.assertTrue(
            {self.public_kb.id, self.dept_kb.id, self.private_kb.id} <= ids)

    def test_detail_of_invisible_kb_returns_404(self):
        self.client.force_authenticate(user=self.outsider)
        resp = self.client.get(f'/api/knowledge/bases/{self.dept_kb.id}/')
        self.assertEqual(resp.status_code, 404)


class KnowledgeScopeSerializerTests(TestCase):
    """scope/department 序列化：旧客户端 is_public 映射 + 部门必填校验"""

    def test_legacy_is_public_false_maps_to_private(self):
        serializer = KnowledgeBaseSerializer(data={'name': '旧库', 'is_public': False})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['scope'], 'private')

    def test_legacy_is_public_true_maps_to_public(self):
        serializer = KnowledgeBaseSerializer(data={'name': '旧库', 'is_public': True})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['scope'], 'public')

    def test_department_scope_requires_department(self):
        serializer = KnowledgeBaseSerializer(data={'name': '部门库', 'scope': 'department'})
        self.assertFalse(serializer.is_valid())
        self.assertIn('department', serializer.errors)


class KnowledgeScopeDocumentsTests(TestCase):
    """文档/上传/问答路径上的可见性：不可见知识库一律 404/不出现"""

    def setUp(self):
        self.media_dir = tempfile.mkdtemp()
        self.owner = User.objects.create_user(
            username='doc_owner', password='demo-pass-9x2', department='技术部')
        self.outsider = User.objects.create_user(
            username='doc_outsider', password='demo-pass-9x2', department='市场部')
        self.public_kb = KnowledgeBase.objects.create(
            name='公开库', scope='public', created_by=self.owner)
        self.dept_kb = KnowledgeBase.objects.create(
            name='部门库', scope='department', department='技术部', created_by=self.owner)
        self.public_doc = Document.objects.create(
            knowledge_base=self.public_kb, title='公开文档', file_name='a.txt',
            file_type='txt', file_size=10, file_path='documents/a.txt', status='completed',
        )
        self.dept_doc = Document.objects.create(
            knowledge_base=self.dept_kb, title='部门文档', file_name='b.txt',
            file_type='txt', file_size=10, file_path='documents/b.txt', status='completed',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.outsider)

    def test_document_list_excludes_invisible_kb_documents(self):
        resp = self.client.get('/api/knowledge/documents/')
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = {item['id'] for item in resp.data['results']}
        self.assertIn(self.public_doc.id, ids)
        self.assertNotIn(self.dept_doc.id, ids)

    @patch('backend.apps.knowledge.views.process_document_task')
    def test_upload_to_invisible_kb_returns_404(self, mock_task):
        upload = SimpleUploadedFile('c.txt', '内容'.encode('utf-8'), content_type='text/plain')
        with override_settings(MEDIA_ROOT=self.media_dir):
            resp = self.client.post('/api/knowledge/documents/upload/', {
                'knowledge_base_id': self.dept_kb.id,
                'file': upload,
            }, format='multipart')
        self.assertEqual(resp.status_code, 404)
        mock_task.delay.assert_not_called()

    def test_ask_invisible_kb_returns_404(self):
        resp = self.client.post('/api/knowledge/chat/ask/', {
            'question': '部门预算是多少？',
            'knowledge_base_id': self.dept_kb.id,
        }, format='json')
        self.assertEqual(resp.status_code, 404)


class KnowledgeWriteOwnershipTests(TestCase):
    """写权限收口：可见范围只决定读，改/删/上传/重解析/分块禁检仅属主或平台管理员"""

    def setUp(self):
        self.media_dir = tempfile.mkdtemp()
        self.owner = User.objects.create_user(
            username='kb_owner', password='demo-pass-9x2', department='技术部')
        self.colleague = User.objects.create_user(
            username='kb_colleague', password='demo-pass-9x2', department='技术部')
        self.outsider = User.objects.create_user(
            username='kb_outsider', password='demo-pass-9x2', department='市场部')
        self.admin = User.objects.create_user(
            username='kb_root', password='demo-pass-9x2', is_staff=True)

        self.public_kb = KnowledgeBase.objects.create(
            name='公开库', scope='public', created_by=self.owner)
        self.dept_kb = KnowledgeBase.objects.create(
            name='部门库', scope='department', department='技术部', created_by=self.owner)
        self.public_doc = self._doc(self.public_kb, '公开文档')
        self.dept_doc = self._doc(self.dept_kb, '部门文档')
        self.dept_chunk = DocumentChunk.objects.create(
            document=self.dept_doc, chunk_index=0, content='部门片段')
        self.client = APIClient()

    @staticmethod
    def _doc(kb, title):
        return Document.objects.create(
            knowledge_base=kb, title=title, file_name=f'{title}.txt',
            file_type='txt', file_size=10, file_path=f'documents/{title}.txt',
            status='completed',
        )

    def _login(self, user):
        self.client.force_authenticate(user=user)
        return self.client

    # ─── 读不受影响：可见范围内仍可读 ───

    def test_visible_documents_still_readable(self):
        self._login(self.colleague)
        self.assertEqual(
            self.client.get(f'/api/knowledge/documents/{self.dept_doc.id}/').status_code, 200)
        self._login(self.outsider)
        self.assertEqual(
            self.client.get(f'/api/knowledge/documents/{self.public_doc.id}/').status_code, 200)

    # ─── 公开库：他人只读 ───

    def test_outsider_cannot_update_public_document(self):
        self._login(self.outsider)
        resp = self.client.patch(
            f'/api/knowledge/documents/{self.public_doc.id}/', {'title': '篡改'}, format='json')
        self.assertEqual(resp.status_code, 403, resp.content)
        self.public_doc.refresh_from_db()
        self.assertEqual(self.public_doc.title, '公开文档')

    def test_outsider_cannot_delete_public_document(self):
        self._login(self.outsider)
        self.assertEqual(
            self.client.delete(f'/api/knowledge/documents/{self.public_doc.id}/').status_code, 403)
        self.assertEqual(
            self.client.delete(f'/api/knowledge/documents/{self.public_doc.id}/delete/').status_code, 403)
        self.assertTrue(Document.objects.filter(id=self.public_doc.id).exists())

    def test_outsider_cannot_update_public_kb(self):
        self._login(self.outsider)
        resp = self.client.patch(
            f'/api/knowledge/bases/{self.public_kb.id}/', {'name': '篡改库'}, format='json')
        self.assertEqual(resp.status_code, 403, resp.content)
        self.public_kb.refresh_from_db()
        self.assertEqual(self.public_kb.name, '公开库')

    def test_outsider_cannot_upload_to_public_kb(self):
        self._login(self.outsider)
        before = Document.objects.count()
        upload = SimpleUploadedFile('evil.txt', '内容'.encode('utf-8'), content_type='text/plain')
        with override_settings(MEDIA_ROOT=self.media_dir):
            resp = self.client.post('/api/knowledge/documents/upload/', {
                'knowledge_base_id': self.public_kb.id, 'file': upload,
            }, format='multipart')
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(Document.objects.count(), before)

    # ─── 部门共享库：同部门成员只读，不能改他人文档 ───

    def test_colleague_cannot_modify_department_kb(self):
        self._login(self.colleague)
        self.assertEqual(
            self.client.patch(f'/api/knowledge/bases/{self.dept_kb.id}/',
                              {'name': '篡改库'}, format='json').status_code, 403)
        self.assertEqual(
            self.client.delete(f'/api/knowledge/bases/{self.dept_kb.id}/').status_code, 403)
        self.assertTrue(KnowledgeBase.objects.filter(id=self.dept_kb.id).exists())

    def test_colleague_cannot_modify_department_document(self):
        self._login(self.colleague)
        self.assertEqual(
            self.client.patch(f'/api/knowledge/documents/{self.dept_doc.id}/',
                              {'title': '篡改'}, format='json').status_code, 403)
        self.assertEqual(
            self.client.post(f'/api/knowledge/documents/{self.dept_doc.id}/reprocess/').status_code, 403)
        self.assertEqual(
            self.client.patch(
                f'/api/knowledge/documents/{self.dept_doc.id}/chunks/{self.dept_chunk.id}/',
                {'is_active': False}, format='json').status_code, 403)
        self.dept_chunk.refresh_from_db()
        self.assertTrue(self.dept_chunk.is_active)

    def test_cannot_create_document_in_others_kb(self):
        self._login(self.outsider)
        resp = self.client.post('/api/knowledge/documents/', {
            'knowledge_base': self.public_kb.id,
        }, format='json')
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_cannot_move_own_document_into_others_kb(self):
        own_kb = KnowledgeBase.objects.create(
            name='同事私有库', scope='private', created_by=self.colleague)
        own_doc = self._doc(own_kb, '同事文档')
        self._login(self.colleague)
        resp = self.client.patch(
            f'/api/knowledge/documents/{own_doc.id}/',
            {'knowledge_base': self.public_kb.id}, format='json')
        self.assertEqual(resp.status_code, 403, resp.content)
        own_doc.refresh_from_db()
        self.assertEqual(own_doc.knowledge_base_id, own_kb.id)

    # ─── 属主 / 管理员可通 ───

    @patch('backend.apps.knowledge.views.process_document_task')
    def test_owner_can_manage_documents(self, mock_task):
        self._login(self.owner)
        upload = SimpleUploadedFile('ok.txt', '内容'.encode('utf-8'), content_type='text/plain')
        with override_settings(MEDIA_ROOT=self.media_dir):
            resp = self.client.post('/api/knowledge/documents/upload/', {
                'knowledge_base_id': self.dept_kb.id, 'file': upload,
            }, format='multipart')
        self.assertEqual(resp.status_code, 201, resp.content)

        self.assertEqual(
            self.client.patch(f'/api/knowledge/documents/{self.dept_doc.id}/',
                              {'title': '改标题'}, format='json').status_code, 200)
        self.assertEqual(
            self.client.post(f'/api/knowledge/documents/{self.dept_doc.id}/reprocess/').status_code, 200)
        self.assertEqual(
            self.client.patch(
                f'/api/knowledge/documents/{self.dept_doc.id}/chunks/{self.dept_chunk.id}/',
                {'is_active': False}, format='json').status_code, 200)
        self.assertEqual(
            self.client.delete(f'/api/knowledge/documents/{self.dept_doc.id}/delete/').status_code, 200)
        self.assertFalse(Document.objects.filter(id=self.dept_doc.id).exists())

    def test_admin_can_manage_others_kb(self):
        self._login(self.admin)
        self.assertEqual(
            self.client.patch(f'/api/knowledge/bases/{self.dept_kb.id}/',
                              {'name': '管理员改名'}, format='json').status_code, 200)
        self.assertEqual(
            self.client.delete(f'/api/knowledge/documents/{self.public_doc.id}/').status_code, 204)
        self.assertFalse(Document.objects.filter(id=self.public_doc.id).exists())


class ReprocessDocumentTests(TestCase):
    """文档重新解析：重置状态并重新投递处理任务；处理中文档拒绝"""

    def setUp(self):
        self.user = User.objects.create_user(username='reproc', password='demo-pass-9x2')
        self.kb = KnowledgeBase.objects.create(name='重解库', created_by=self.user)
        self.doc = Document.objects.create(
            knowledge_base=self.kb, title='手册', file_name='m.txt',
            file_type='txt', file_size=10, file_path='documents/m.txt',
            status='failed', error_msg='上次失败原因',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch('backend.apps.knowledge.views.process_document_task')
    def test_reprocess_resets_status_and_enqueues_task(self, mock_task):
        # eager 模式禁用：mock 后任务不会真跑，状态停留在 pending
        resp = self.client.post(f'/api/knowledge/documents/{self.doc.id}/reprocess/')
        self.assertEqual(resp.status_code, 200, resp.content)

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, 'pending')
        self.assertEqual(self.doc.error_msg, '')
        mock_task.delay.assert_called_once_with(self.doc.id)

    @patch('backend.apps.knowledge.views.process_document_task')
    def test_reprocess_rejects_processing_document(self, mock_task):
        self.doc.status = 'processing'
        self.doc.save(update_fields=['status'])

        resp = self.client.post(f'/api/knowledge/documents/{self.doc.id}/reprocess/')
        self.assertEqual(resp.status_code, 400)
        mock_task.delay.assert_not_called()

    def test_reprocess_requires_authentication(self):
        client = APIClient()
        resp = client.post(f'/api/knowledge/documents/{self.doc.id}/reprocess/')
        self.assertEqual(resp.status_code, 401)


class CleanTextTests(SimpleTestCase):
    """OCR 正文与文字层正文共用同一套清洗口径"""

    def test_nfkc_and_control_chars_removed(self):
        self.assertEqual(clean_text('ＡＢＣ\u0000  你好\t世界'), 'ABC 你好 世界')

    def test_crlf_normalized_and_blank_lines_compressed(self):
        self.assertEqual(clean_text('a\r\n\r\n\r\n\r\nb'), 'a\n\nb')

    def test_line_padding_trimmed(self):
        self.assertEqual(clean_text('  第一行  \n\t第二行\t'), '第一行\n第二行')


class ExtractPagesTests(SimpleTestCase):
    """逐页判定：有文字层直接用，无文字层才 OCR；混合文档逐页各走各的"""

    def _pdf_pages(self, texts):
        pages = []
        for text in texts:
            page = MagicMock()
            page.extract_text.return_value = text
            pages.append(page)
        return pages

    @patch('backend.apps.knowledge.services.ocr.ocr_pdf_page')
    @patch('backend.apps.knowledge.services.PdfReader')
    def test_mixed_pdf_uses_text_layer_and_ocr_per_page(self, mock_reader, mock_ocr):
        mock_reader.return_value.pages = self._pdf_pages(
            ['第一页是有文字层的内容，长度足够不触发 OCR', ''])
        mock_ocr.return_value = OcrOutcome(text='第二页扫描识别结果', engine='llm')

        pages, report = extract_pages('mixed.pdf', 'pdf')

        self.assertEqual(len(pages), 2)
        self.assertFalse(pages[0].ocr)
        self.assertEqual(pages[0].text, clean_text('第一页是有文字层的内容，长度足够不触发 OCR'))
        self.assertTrue(pages[1].ocr)
        self.assertEqual(pages[1].text, '第二页扫描识别结果')
        self.assertEqual(pages[1].engine, 'llm')
        mock_ocr.assert_called_once_with('mixed.pdf', 1)
        self.assertEqual(report['mode'], 'auto')
        self.assertEqual(report['pages_total'], 2)
        self.assertEqual(report['ocr_pages'], [2])
        self.assertTrue(report['ocr_used'])
        self.assertEqual(report['failed_pages'], [])

    @patch('backend.apps.knowledge.services.ocr.ocr_pdf_page')
    @patch('backend.apps.knowledge.services.PdfReader')
    def test_text_layer_pdf_never_calls_ocr(self, mock_reader, mock_ocr):
        mock_reader.return_value.pages = self._pdf_pages(
            ['第一页文字层内容足够长不触发 OCR', '第二页文字层内容也足够长不触发 OCR'])

        pages, report = extract_pages('text.pdf', 'pdf')

        self.assertTrue(all(not p.ocr for p in pages))
        mock_ocr.assert_not_called()
        self.assertEqual(report['ocr_pages'], [])
        self.assertEqual(report['failed_pages'], [])

    @patch('backend.apps.knowledge.services.ocr.ocr_pdf_page')
    @patch('backend.apps.knowledge.services.PdfReader')
    def test_short_text_layer_kept_when_ocr_fails(self, mock_reader, mock_ocr):
        mock_reader.return_value.pages = self._pdf_pages(['短'])
        mock_ocr.return_value = OcrOutcome(error='llm=llm_not_configured；tesseract=tesseract_not_installed')

        pages, report = extract_pages('short.pdf', 'pdf')

        self.assertEqual(pages[0].text, '短')
        self.assertEqual(pages[0].status, 'ok')
        self.assertIn('ocr_failed:', pages[0].reason)
        self.assertEqual(report['failed_pages'], [])

    @patch('backend.apps.knowledge.services.ocr.ocr_pdf_page')
    @patch('backend.apps.knowledge.services.PdfReader')
    def test_ocr_disabled_does_not_call_ocr(self, mock_reader, mock_ocr):
        mock_reader.return_value.pages = self._pdf_pages([''])

        pages, report = extract_pages('scan.pdf', 'pdf', enable_ocr=False)

        mock_ocr.assert_not_called()
        self.assertEqual(pages[0].status, 'failed')
        self.assertEqual(pages[0].reason, 'ocr_disabled')
        self.assertEqual(report['failed_pages'], [{'page': 1, 'reason': 'ocr_disabled'}])

    @patch('backend.apps.knowledge.services.PdfReader')
    def test_max_pages_marks_remaining_failed(self, mock_reader):
        mock_reader.return_value.pages = self._pdf_pages(['内容足够长的一句正文', '另一页正文', '第三页'])

        with self.settings(KNOWLEDGE_OCR_MAX_PAGES=2):
            pages, report = extract_pages('long.pdf', 'pdf', enable_ocr=False)

        self.assertEqual(len(pages), 3)
        self.assertEqual(pages[-1].status, 'failed')
        self.assertEqual(pages[-1].reason, 'max_pages_exceeded')

    @patch('backend.apps.knowledge.services.ocr.ocr_image_file')
    def test_image_uses_ocr_when_enabled(self, mock_ocr):
        mock_ocr.return_value = OcrOutcome(text='图片里的文字', engine='tesseract')

        pages, report = extract_pages('scan.png', 'png')

        mock_ocr.assert_called_once_with('scan.png', 'png')
        self.assertTrue(pages[0].ocr)
        self.assertEqual(pages[0].text, '图片里的文字')
        self.assertEqual(report['ocr_pages'], [1])

    @patch('backend.apps.knowledge.services.ocr.ocr_image_file')
    def test_image_with_ocr_disabled_is_unresolved(self, mock_ocr):
        pages, report = extract_pages('scan.png', 'png', enable_ocr=False)

        mock_ocr.assert_not_called()
        self.assertEqual(pages[0].status, 'failed')
        self.assertEqual(report['failed_pages'], [{'page': 1, 'reason': 'ocr_disabled'}])

    def test_text_document_report_has_no_pages(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.txt')
        os.close(fd)
        try:
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('普通文本正文')
            pages, report = extract_pages(path, 'txt')
        finally:
            os.unlink(path)

        self.assertEqual(pages[0].page, None)
        self.assertEqual(report['mode'], 'text')
        self.assertEqual(report['pages_total'], 0)
        self.assertEqual(report['pages'], [])


class OcrFallbackTests(SimpleTestCase):
    """auto 三级回退：多模态模型 → tesseract → 明确失败（逐级原因）"""

    @patch('backend.apps.knowledge.ocr._tesseract_ocr')
    @patch('backend.apps.knowledge.ocr._llm_ocr')
    def test_llm_success_short_circuits(self, mock_llm, mock_tess):
        mock_llm.return_value = ('模型识别文本', '')

        outcome = ocr_image_bytes(b'img', 'image/png')

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.engine, 'llm')
        self.assertEqual(outcome.text, '模型识别文本')
        mock_tess.assert_not_called()

    @patch('backend.apps.knowledge.ocr._tesseract_ocr')
    @patch('backend.apps.knowledge.ocr._llm_ocr')
    def test_falls_back_to_tesseract(self, mock_llm, mock_tess):
        mock_llm.return_value = ('', 'llm_not_configured')
        mock_tess.return_value = ('本机识别文本', '')

        outcome = ocr_image_bytes(b'img', 'image/png')

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.engine, 'tesseract')
        self.assertEqual([a['engine'] for a in outcome.attempts], ['llm', 'tesseract'])
        self.assertEqual(outcome.attempts[0]['error'], 'llm_not_configured')

    @patch('backend.apps.knowledge.ocr._tesseract_ocr')
    @patch('backend.apps.knowledge.ocr._llm_ocr')
    def test_both_fail_reports_both_reasons(self, mock_llm, mock_tess):
        mock_llm.return_value = ('', 'llm_not_configured')
        mock_tess.return_value = ('', 'tesseract_not_installed')

        outcome = ocr_image_bytes(b'img', 'image/png')

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.engine, '')
        self.assertEqual(outcome.error,
                         'llm=llm_not_configured；tesseract=tesseract_not_installed')

    @patch('backend.apps.knowledge.ocr.shutil.which', return_value=None)
    def test_missing_tesseract_is_reported(self, _mock_which):
        self.assertEqual(tesseract_status(), (False, 'tesseract_not_installed'))


class ProcessDocumentOcrTests(TestCase):
    """OCR 入库：切片 meta 带页码/OCR 标记；双失败不出现 completed 空壳"""

    def setUp(self):
        self.media_dir = tempfile.mkdtemp()
        self.kb = KnowledgeBase.objects.create(name='OCR 库')

    def _make_doc(self, file_name: str, file_type: str) -> Document:
        return Document.objects.create(
            knowledge_base=self.kb, title='OCR 文档', file_name=file_name,
            file_type=file_type, file_size=100, file_path=file_name, status='pending',
        )

    @patch('backend.apps.knowledge.services.vectorstore')
    @patch('backend.apps.knowledge.services.embeddings')
    @patch('backend.apps.knowledge.services.ocr.ocr_image_file')
    def test_image_ocr_chunk_meta_has_page_and_engine(self, mock_ocr, mock_emb, mock_vs):
        mock_vs.is_enabled.return_value = False
        mock_emb.return_value = [[1.0]]
        mock_ocr.return_value = OcrOutcome(text='发票号码 12345', engine='tesseract')

        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc('scan.png', 'png')
            process_document(doc)

        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed')
        self.assertEqual(doc.chunk_count, 1)
        chunk = doc.chunks.get()
        self.assertEqual(chunk.content, '发票号码 12345')
        self.assertEqual(chunk.meta, {'page': 1, 'ocr': True, 'ocr_engine': 'tesseract'})
        # OCR 标记不进正文：正文里不出现页码/引擎字样
        self.assertNotIn('OCR', chunk.content)
        self.assertEqual(doc.extraction_report['ocr_pages'], [1])
        self.assertTrue(doc.extraction_report['ocr_used'])

    @patch('backend.apps.knowledge.services.vectorstore')
    @patch('backend.apps.knowledge.services.embeddings')
    @patch('backend.apps.knowledge.services.ocr.ocr_image_file')
    def test_all_ocr_failures_mark_failed_with_page_reason(self, mock_ocr, mock_emb, mock_vs):
        mock_vs.is_enabled.return_value = False
        mock_ocr.return_value = OcrOutcome(
            error='llm=llm_not_configured；tesseract=tesseract_not_installed')

        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc('scan.png', 'png')
            process_document(doc)

        doc.refresh_from_db()
        self.assertEqual(doc.status, 'failed')
        self.assertEqual(doc.chunk_count, 0)
        self.assertEqual(DocumentChunk.objects.filter(document=doc).count(), 0)
        self.assertIn('第 1 页', doc.error_msg)
        self.assertIn('tesseract_not_installed', doc.error_msg)
        self.assertEqual(doc.extraction_report['failed_pages'], [{
            'page': 1,
            'reason': 'llm=llm_not_configured；tesseract=tesseract_not_installed',
        }])
        mock_emb.assert_not_called()
        self.kb.refresh_from_db()
        self.assertEqual(self.kb.chunk_count, 0)

    @patch('backend.apps.knowledge.services.vectorstore')
    @patch('backend.apps.knowledge.services.embeddings')
    @patch('backend.apps.knowledge.services.ocr.ocr_pdf_page')
    @patch('backend.apps.knowledge.services.PdfReader')
    def test_mixed_pdf_chunks_carry_their_own_page_meta(
        self, mock_reader, mock_ocr, mock_emb, mock_vs,
    ):
        page1 = MagicMock()
        page1.extract_text.return_value = '第一页文字层内容足够长，不触发 OCR'
        page2 = MagicMock()
        page2.extract_text.return_value = ''
        mock_reader.return_value.pages = [page1, page2]
        mock_ocr.return_value = OcrOutcome(text='第二页扫描识别内容', engine='llm')
        mock_vs.is_enabled.return_value = False
        mock_emb.return_value = [[1.0], [0.0]]

        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc('mixed.pdf', 'pdf')
            process_document(doc)

        doc.refresh_from_db()
        self.assertEqual(doc.status, 'completed')
        chunks = list(doc.chunks.order_by('chunk_index'))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].meta, {'page': 1})
        self.assertEqual(chunks[1].meta, {'page': 2, 'ocr': True, 'ocr_engine': 'llm'})
        self.assertEqual(doc.extraction_report['ocr_pages'], [2])

    @patch('backend.apps.knowledge.services.vectorstore')
    @patch('backend.apps.knowledge.services.embeddings')
    @patch('backend.apps.knowledge.services.ocr.ocr_pdf_page')
    @patch('backend.apps.knowledge.services.PdfReader')
    def test_ocr_and_text_layer_share_dedupe(
        self, mock_reader, mock_ocr, mock_emb, mock_vs,
    ):
        same = '这是一段重复出现的正文内容用于验证去重口径'
        page1 = MagicMock()
        page1.extract_text.return_value = same
        page2 = MagicMock()
        page2.extract_text.return_value = ''
        mock_reader.return_value.pages = [page1, page2]
        mock_ocr.return_value = OcrOutcome(text=same, engine='tesseract')
        mock_vs.is_enabled.return_value = False
        mock_emb.return_value = [[1.0]]

        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc('dup.pdf', 'pdf')
            process_document(doc)

        doc.refresh_from_db()
        self.assertEqual(doc.chunk_count, 1)
        chunk = doc.chunks.get()
        self.assertEqual(chunk.content, same)
        # 保留首次出现的文字层页，OCR 页重复内容被去重
        self.assertEqual(chunk.meta, {'page': 1})

    @patch('backend.apps.knowledge.services.vectorstore')
    @patch('backend.apps.knowledge.services.embeddings')
    @patch('backend.apps.knowledge.services.ocr.ocr_image_file')
    def test_conversation_attachments_never_ocr(self, mock_ocr, mock_emb, mock_vs):
        """对话临时附件路径传 enable_ocr=False：不调用 OCR，无内容即 failed"""
        mock_vs.is_enabled.return_value = False

        with override_settings(MEDIA_ROOT=self.media_dir):
            doc = self._make_doc('scan.png', 'png')
            process_document(doc, enable_ocr=False)

        doc.refresh_from_db()
        mock_ocr.assert_not_called()
        mock_emb.assert_not_called()
        self.assertEqual(doc.status, 'failed')
        self.assertEqual(doc.extraction_report['failed_pages'], [{
            'page': 1, 'reason': 'ocr_disabled',
        }])


class KnowledgeTaskQueueTests(SimpleTestCase):
    """knowledge 队列与任务超时参数：lasso-worker 不再消费文档处理任务"""

    def test_task_routed_to_knowledge_queue(self):
        route = settings.CELERY_TASK_ROUTES[
            'backend.apps.knowledge.tasks.process_document_task']
        self.assertEqual(route['queue'], 'knowledge')

    def test_task_uses_late_ack_and_time_limits(self):
        self.assertTrue(process_document_task.acks_late)
        self.assertEqual(process_document_task.soft_time_limit, 900)
        self.assertEqual(process_document_task.time_limit, 960)

    def test_tiangong_task_declares_knowledge_queue(self):
        self.assertEqual(tiangong_run_task.queue, 'knowledge')
        route = settings.CELERY_TASK_ROUTES[
            'backend.apps.knowledge.tasks.tiangong_run_task']
        self.assertEqual(route['queue'], 'knowledge')


class TiangongDiscoveryUnitTests(TestCase):
    """L4 发现/打分/质量过滤：纯函数与 sitemap 解析"""

    def test_keywords_strip_generic_suffix(self):
        words = discovery.keywords_of('工伤认定流程，停工留薪期、劳动能力鉴定办法')
        self.assertIn('工伤认定流程', words)
        self.assertIn('工伤认定', words)
        self.assertIn('停工留薪期', words)
        self.assertIn('劳动能力鉴定', words)

    def test_topic_score_weights_url_title_body(self):
        score = discovery.topic_score(['工伤认定'], url='https://x.gov.cn/工伤认定',
                                      title='工伤认定申请指南', body='工伤认定需要材料')
        self.assertEqual(score, 2 + 3 + 1)

    def test_source_topic_gate_skips_unrelated(self):
        self.assertFalse(discovery.source_matches('税务 发票', ['工伤认定']))
        self.assertTrue(discovery.source_matches('工伤 社保', ['工伤认定']))
        self.assertTrue(discovery.source_matches('', ['工伤认定']))

    def test_quality_issues(self):
        self.assertEqual(discovery.quality_issue(url='https://x/a', title='t', text='短'), '正文过短')
        self.assertEqual(
            discovery.quality_issue(url='https://x/a', title='404 页面不存在', text='a' * 200),
            '疑似错误页')
        self.assertEqual(
            discovery.quality_issue(url='https://x/a', title='登录', text='请登录后查看' * 20),
            '疑似登录页')

    @patch('backend.apps.knowledge.discovery.net.http_get')
    def test_sitemap_keeps_same_host_https_html_only(self, mock_get):
        xml = (
            '<urlset>'
            '<url><loc>https://www.example.com/a</loc></url>'
            '<url><loc>https://other.com/b</loc></url>'
            '<url><loc>http://www.example.com/c</loc></url>'
            '<url><loc>https://www.example.com/logo.png</loc></url>'
            '</urlset>'
        )
        mock_get.return_value = (xml.encode('utf-8'), {})
        locs = discovery._sitemap_locs('example.com')
        self.assertEqual(locs, ['https://www.example.com/a'])

    @patch('backend.apps.knowledge.discovery._sitemap_locs', return_value=[])
    @patch('backend.apps.knowledge.discovery._throttle')
    @patch('backend.apps.knowledge.discovery.fetch_page')
    def test_crawl_respects_per_source_page_cap(self, mock_fetch, _throttle, _sitemap):
        def page(url):
            return {
                'url': url, 'title': '工伤认定指南', 'text': '正文' * 200,
                'html': '<a href="/p2">工伤认定</a>', 'content_sha256': url,
                'site': 'example.com', 'links': [], 'link_count': 0,
            }
        mock_fetch.side_effect = page
        source = TiangongSource(name='示例', domain='example.com',
                                entry_urls='https://example.com/a', topic_tags='工伤认定')
        result = discovery.crawl_source(source, ['工伤认定'], discovery.CrawlBudget(8),
                                        max_pages=3)
        self.assertEqual(result['fetched'], 1)
        self.assertEqual(len(result['pages']), 1)

    def _dns_result(self, records):
        """按 family 返回解析结果：AF_INET 只给 A 记录，AF_UNSPEC 才带 AAAA"""
        def fake(host, port=None, family=0, type=0, proto=0, flags=0):
            if family == socket.AF_INET:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                         (addr, port or 0)) for addr in records.get('a', [])]
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', (addr, port or 0))
                for addr in records.get('a', [])
            ] + [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, '', (addr, port or 0, 0, 0))
                for addr in records.get('aaaa', [])
            ]
        return fake

    @patch('backend.apps.mcp.services.socket.getaddrinfo')
    def test_dns_public_a_allows_even_with_non_global_aaaa(self, mock_dns):
        mock_dns.side_effect = self._dns_result(
            {'a': ['93.184.216.34'], 'aaaa': ['2001::1']})
        self.assertTrue(discovery._dns_is_public('example.com'))
        self.assertEqual(mock_dns.call_args.args[2], socket.AF_INET)

    @patch('backend.apps.mcp.services.socket.getaddrinfo')
    def test_dns_only_aaaa_is_rejected(self, mock_dns):
        mock_dns.side_effect = self._dns_result({'a': [], 'aaaa': ['2001:db8::1']})
        self.assertFalse(discovery._dns_is_public('example.com'))

    @patch('backend.apps.mcp.services.socket.getaddrinfo')
    def test_dns_private_a_with_public_aaaa_is_rejected(self, mock_dns):
        mock_dns.side_effect = self._dns_result(
            {'a': ['172.16.0.8'], 'aaaa': ['2606:4700::1111']})
        self.assertFalse(discovery._dns_is_public('example.com'))

    @patch('backend.apps.nuwa.net.socket.getaddrinfo', return_value=[])
    def test_connection_fails_when_no_a_record(self, mock_dns):
        from backend.apps.nuwa import net

        with self.assertRaises(OSError):
            net._ipv4_create_connection(('example.com', 443))
        self.assertEqual(mock_dns.call_args.args[2], socket.AF_INET)


class TiangongSearxngTests(TestCase):
    """L2：JSON 解析、空结果、熔断与原因码"""

    def _payload(self, results):
        import json
        return json.dumps({'results': results}).encode('utf-8')

    @patch('backend.apps.knowledge.searxng.net.http_get')
    def test_search_normalizes_and_filters_urls(self, mock_get):
        mock_get.return_value = (self._payload([
            {'url': 'https://www.example.com/a', 'title': '工伤认定', 'content': '摘要',
             'engine': 'bing', 'score': 1.5},
            {'url': 'http://www.example.com/b', 'title': '非 https'},
            {'url': 'https://127.0.0.1/c', 'title': '内网'},
        ]), {})
        result = searxng.search(['工伤认定流程'])
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['url'], 'https://www.example.com/a')
        self.assertEqual(result['results'][0]['domain'], 'www.example.com')
        self.assertEqual(result['reason'], '')

    @patch('backend.apps.knowledge.searxng.net.http_get')
    def test_empty_results_increments_failure(self, mock_get):
        mock_get.return_value = (self._payload([]), {})
        result = searxng.search(['工伤'])
        self.assertEqual(result['reason'], 'searxng_empty')
        self.assertEqual(searxng.circuit_state()['fail_count'], 1)

    @patch('backend.apps.knowledge.searxng.net.http_get')
    def test_three_failures_open_circuit_and_stop_requests(self, mock_get):
        from backend.apps.nuwa import net

        mock_get.side_effect = net.RemoteError('timeout', 'timeout')
        for _ in range(3):
            self.assertEqual(searxng.search(['工伤'])['reason'], 'searxng_timeout')
        state = searxng.circuit_state()
        self.assertTrue(state['open'])
        mock_get.reset_mock()
        result = searxng.search(['工伤'])
        self.assertEqual(result['reason'], 'searxng_circuit_open')
        mock_get.assert_not_called()

    def test_disabled_returns_reason(self):
        SystemSetting.set_value(searxng.KEY_ENABLED, 'false')
        self.assertEqual(searxng.search(['工伤'])['reason'], 'searxng_disabled')


class TiangongSourceApiTests(TestCase):
    """来源库/候选审批/设置接口：权限与审批入库"""

    def setUp(self):
        self.admin = User.objects.create_user(username='admin', password='x', is_staff=True)
        self.user = User.objects.create_user(username='user', password='x')
        self.kb = KnowledgeBase.objects.create(name='工伤库')
        self.run = TiangongRun.objects.create(knowledge_base=self.kb, topics='工伤认定')
        self.proposal = TiangongSourceProposal.objects.create(
            run=self.run, knowledge_base=self.kb, source_type='government',
            discovered_by='searxng', domain='example.gov.cn',
            url='https://example.gov.cn/policy', title='工伤政策', topics='工伤认定')

    def test_non_admin_cannot_create_source(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        resp = client.post('/api/knowledge/tiangong-sources/',
                           {'name': '示例', 'domain': 'example.gov.cn'}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_admin_creates_source_and_audits(self):
        client = APIClient()
        client.force_authenticate(user=self.admin)
        resp = client.post('/api/knowledge/tiangong-sources/',
                           {'name': '示例', 'domain': 'https://Example.gov.cn/path'},
                           format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data['domain'], 'example.gov.cn')
        self.assertTrue(AuditLog.objects.filter(action='tiangong_source_create').exists())

    def test_approve_creates_source_and_reject_does_not(self):
        client = APIClient()
        client.force_authenticate(user=self.admin)
        resp = client.post(
            f'/api/knowledge/tiangong-source-proposals/{self.proposal.id}/approve/')
        self.assertEqual(resp.status_code, 200, resp.content)
        source = TiangongSource.objects.get(domain='example.gov.cn')
        self.assertEqual(source.entry_urls, 'https://example.gov.cn/policy')
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, 'approved')

        other = TiangongSourceProposal.objects.create(
            run=self.run, knowledge_base=self.kb, domain='other.com',
            url='https://other.com/a', discovered_by='searxng')
        resp = client.post(
            f'/api/knowledge/tiangong-source-proposals/{other.id}/reject/',
            {'reason': '不相关'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(TiangongSource.objects.filter(domain='other.com').exists())
        other.refresh_from_db()
        self.assertEqual(other.status, 'rejected')
        self.assertEqual(other.reject_reason, '不相关')

    def test_double_approve_is_rejected(self):
        client = APIClient()
        client.force_authenticate(user=self.admin)
        url = f'/api/knowledge/tiangong-source-proposals/{self.proposal.id}/approve/'
        self.assertEqual(client.post(url).status_code, 200)
        self.assertEqual(client.post(url).status_code, 400)

    def test_search_settings_admin_only(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        self.assertEqual(client.get('/api/knowledge/tiangong-search/settings/').status_code, 403)
        client.force_authenticate(user=self.admin)
        resp = client.post('/api/knowledge/tiangong-search/settings/',
                           {'searxng_enabled': False, 'max_queries': 2}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data['searxng_enabled'])
        self.assertEqual(resp.data['max_queries'], 2)


class TiangongRunExecuteTests(TestCase):
    """闭环：搜索只产候选、未审批不抓、批准来源才抓正文"""

    def setUp(self):
        self.media_dir = tempfile.mkdtemp()
        self.kb = KnowledgeBase.objects.create(
            name='工伤库', tiangong_enabled=True, tiangong_topics='工伤认定流程')
        self.run = TiangongRun.objects.create(knowledge_base=self.kb,
                                              topics='工伤认定流程', status='queued')

    @patch('backend.apps.knowledge.tiangong._collect_web')
    @patch('backend.apps.knowledge.tiangong._collect_model', return_value=(0, ''))
    @patch('backend.apps.knowledge.tiangong._search_searxng')
    @patch('backend.apps.knowledge.tiangong._search_mcp', return_value=([], 'mcp_not_configured', 0))
    def test_searxng_hit_creates_pending_proposals_only(
        self, _mcp, mock_searxng, _model, mock_collect,
    ):
        mock_collect.return_value = {'created': 0, 'skipped': 0, 'pages': 0,
                                     'rejected': 0, 'sources': 0, 'degraded': ''}
        mock_searxng.return_value = ([
            {'url': 'https://example.gov.cn/a', 'title': '工伤认定', 'snippet': '摘要',
             'domain': 'example.gov.cn', 'score': 2.0},
            {'url': 'https://news.example.com/b', 'title': '工伤新闻', 'snippet': '',
             'domain': 'news.example.com', 'score': 1.0},
        ], '', 120)
        tiangong.execute(self.run.id)

        self.run.refresh_from_db()
        self.assertEqual(self.run.discovery_layer, 'l2_searxng')
        self.assertEqual(self.run.search_hits, 2)
        self.assertEqual(self.run.candidates, 2)
        self.assertEqual(self.run.proposals, 2)
        self.assertEqual(self.run.status, 'done')
        self.assertEqual(self.run.fallback_chain[0]['reason'], 'mcp_not_configured')
        proposals = TiangongSourceProposal.objects.filter(run=self.run)
        self.assertEqual(proposals.count(), 2)
        self.assertTrue(all(p.status == 'pending' for p in proposals))
        self.assertTrue(all(p.discovered_by == 'searxng' for p in proposals))
        self.assertEqual(
            TiangongSourceProposal.objects.get(domain='example.gov.cn').source_type,
            'government')

    @patch('backend.apps.knowledge.tiangong.discovery.fetch_page')
    @patch('backend.apps.knowledge.tiangong._collect_model', return_value=(0, ''))
    @patch('backend.apps.knowledge.tiangong._search_searxng', return_value=([], 'searxng_empty', 30))
    @patch('backend.apps.knowledge.tiangong._search_mcp', return_value=([], 'mcp_not_configured', 0))
    def test_pending_proposals_are_never_crawled(self, _mcp, _searxng, _model, mock_fetch):
        TiangongSourceProposal.objects.create(
            run=self.run, knowledge_base=self.kb, domain='example.gov.cn',
            url='https://example.gov.cn/a', discovered_by='searxng')
        tiangong.execute(self.run.id)
        mock_fetch.assert_not_called()

    @patch('backend.apps.knowledge.tiangong.process_document')
    @patch('backend.apps.knowledge.tiangong.discovery.crawl_source')
    @patch('backend.apps.knowledge.tiangong._collect_model', return_value=(0, ''))
    @patch('backend.apps.knowledge.tiangong._search_searxng', return_value=([], 'searxng_empty', 30))
    @patch('backend.apps.knowledge.tiangong._search_mcp', return_value=([], 'mcp_not_configured', 0))
    def test_approved_source_is_crawled_and_stored(
        self, _mcp, _searxng, _model, mock_crawl, _process,
    ):
        TiangongSource.objects.create(
            name='示例政府', domain='example.gov.cn', entry_urls='https://example.gov.cn/a',
            topic_tags='工伤认定')
        mock_crawl.return_value = {
            'pages': [{
                'url': 'https://example.gov.cn/a', 'title': '工伤认定指南',
                'text': '正文内容' * 50, 'content_sha256': 'a' * 64,
                'site': 'example.gov.cn',
            }],
            'fetched': 1, 'rejected': 0, 'skipped': 0, 'reason': '',
        }
        with override_settings(MEDIA_ROOT=self.media_dir):
            tiangong.execute(self.run.id)

        self.run.refresh_from_db()
        self.assertEqual(self.run.web_created, 1)
        self.assertEqual(self.run.pages_fetched, 1)
        self.assertEqual(self.run.sources_tried, 1)
        doc = Document.objects.get(knowledge_base=self.kb,
                                   source_url='https://example.gov.cn/a')
        self.assertEqual(doc.source_type, 'web')
        self.assertEqual(doc.title, '工伤认定指南')


class TiangongMcpSearchTests(TestCase):
    """L1：候选扫描、绑定可用性、结果映射、降级原因码与审批接口"""

    def setUp(self):
        self.server = MCPServer.objects.create(
            name='搜索来源', slug='search-src', transport='http_mcp',
            url='https://mcp.example.com/mcp', status='healthy')
        self.tool = MCPTool.objects.create(
            server=self.server, name='web_search', full_name='mcp__search-src__web_search',
            description='Search the web and return results',
            input_schema={'type': 'object', 'properties': {
                'query': {'type': 'string'}, 'limit': {'type': 'integer'}},
                'required': ['query']}, enabled=True)
        self.binding = TiangongSearchBinding.objects.create(
            server=self.server, tool=self.tool, query_field='query',
            limit_field='limit', default_limit=5, priority=10)
        self.admin = User.objects.create_user(username='tg-admin', password='x', is_staff=True)

    def test_scan_candidates_scores_search_tool(self):
        MCPTool.objects.create(
            server=self.server, name='fetch_page', full_name='mcp__search-src__fetch_page',
            description='Fetch a single URL', input_schema={'properties': {}})
        candidates = mcp_search.scan_candidates()
        self.assertEqual(len(candidates), 1)
        item = candidates[0]
        self.assertEqual(item['tool_id'], self.tool.id)
        self.assertGreaterEqual(item['score'], 60)
        self.assertEqual(item['suggested_mapping']['query_field'], 'query')
        self.assertEqual(item['suggested_mapping']['limit_field'], 'limit')
        self.assertTrue(item['bound'])

    def test_binding_usable_only_when_tool_enabled(self):
        self.assertTrue(self.binding.usable)
        self.tool.enabled = False
        self.tool.save()
        self.binding.refresh_from_db()
        self.assertFalse(self.binding.enabled)  # 工具停用 → 绑定自动停用
        self.assertEqual(self.binding.unusable_reason, 'mcp_disabled')

    def test_server_disable_auto_disables_binding(self):
        self.server.is_active = False
        self.server.save()
        self.binding.refresh_from_db()
        self.assertFalse(self.binding.enabled)

    def test_extract_results_supports_nested_mapping(self):
        binding = TiangongSearchBinding(
            server=self.server, tool=self.tool, results_path='data.items',
            url_field='link', title_field='name', snippet_field='snippet')
        result = SimpleNamespace(
            structuredContent={'data': {'items': [
                {'link': 'https://www.example.com/a', 'name': '示例',
                 'snippet': '摘要', 'score': 2},
                {'link': 'http://www.example.com/b', 'name': '非 https'},
            ]}}, content=[], isError=False)
        results, raw_count = mcp_search.extract_results(binding, result)
        self.assertEqual(raw_count, 2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['url'], 'https://www.example.com/a')
        self.assertEqual(results[0]['title'], '示例')

    def test_extract_results_parses_text_json(self):
        binding = TiangongSearchBinding(
            server=self.server, tool=self.tool, results_path='results')
        payload = '{"results": [{"url": "https://www.example.com/a", "title": "标题"}]}'
        result = SimpleNamespace(
            structuredContent=None,
            content=[SimpleNamespace(type='text', text=payload)], isError=False)
        results, _raw = mcp_search.extract_results(binding, result)
        self.assertEqual(results[0]['url'], 'https://www.example.com/a')

    def test_search_reason_codes_and_fallback_binding(self):
        TiangongSearchBinding.objects.all().delete()
        self.assertEqual(mcp_search.search(['工伤'])[1], 'mcp_not_configured')

        self.tool.enabled = False
        self.tool.save()
        TiangongSearchBinding.objects.create(server=self.server, tool=self.tool)
        self.assertEqual(mcp_search.search(['工伤'])[1], 'mcp_tool_disabled')

        self.tool.enabled = True
        self.tool.save()
        TiangongSearchBinding.objects.all().update(enabled=True)
        with patch.object(mcp_search, 'call_binding',
                          return_value={'results': [], 'reason': '', 'raw_count': 0,
                                        'elapsed_ms': 3, 'error': ''}):
            self.assertEqual(mcp_search.search(['工伤'])[1], 'mcp_empty')
        with patch.object(mcp_search, 'call_binding',
                          return_value={'results': [], 'reason': 'mcp_timeout',
                                        'raw_count': 0, 'elapsed_ms': 3, 'error': ''}):
            self.assertEqual(mcp_search.search(['工伤'])[1], 'mcp_timeout')

    @patch('backend.apps.knowledge.mcp_search.call_binding')
    def test_search_falls_back_to_next_binding(self, mock_call):
        other_tool = MCPTool.objects.create(
            server=self.server, name='web_search_backup',
            full_name='mcp__search-src__web_search_backup',
            description='web search', input_schema={'properties': {'q': {'type': 'string'}}},
            enabled=True)
        other = TiangongSearchBinding.objects.create(
            server=self.server, tool=other_tool, query_field='q', priority=20)
        payload = [{'url': 'https://www.example.com/a', 'title': '结果'}]
        mock_call.side_effect = [
            {'results': [], 'reason': 'mcp_call_failed', 'raw_count': 0,
             'elapsed_ms': 1, 'error': 'boom'},
            {'results': payload, 'reason': '', 'raw_count': 1,
             'elapsed_ms': 2, 'error': ''},
        ]
        results, reason, _elapsed = mcp_search.search(['工伤'])
        self.assertEqual(reason, '')
        self.assertEqual(results[0]['url'], 'https://www.example.com/a')
        self.assertEqual(mock_call.call_args.args[0].id, other.id)

    def test_mcp_switch_off_returns_disabled(self):
        SystemSetting.set_value(tiangong.KEY_MCP, 'false')
        self.assertEqual(tiangong._search_mcp(['工伤'])[1], 'mcp_disabled')

    def test_candidates_and_binding_api_admin_only(self):
        client = APIClient()
        user = User.objects.create_user(username='plain', password='x')
        client.force_authenticate(user=user)
        self.assertEqual(client.get('/api/knowledge/tiangong-search/candidates/').status_code, 403)

        client.force_authenticate(user=self.admin)
        resp = client.get('/api/knowledge/tiangong-search/candidates/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['candidates'][0]['tool_id'], self.tool.id)

        disabled = MCPTool.objects.create(
            server=self.server, name='web_search_2',
            full_name='mcp__search-src__web_search_2',
            description='search', input_schema={'properties': {'query': {'type': 'string'}}},
            enabled=False)
        resp = client.post('/api/knowledge/tiangong-search-bindings/', {
            'server': self.server.id, 'tool': disabled.id, 'query_field': 'query',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        disabled.refresh_from_db()
        self.assertFalse(disabled.enabled)  # 创建绑定不自动启用工具
        self.assertFalse(resp.data['usable'])

    def test_binding_rejects_tool_from_other_server(self):
        other_server = MCPServer.objects.create(
            name='另一来源', slug='other-src', transport='http_mcp',
            url='https://other.example.com/mcp')
        client = APIClient()
        client.force_authenticate(user=self.admin)
        resp = client.post('/api/knowledge/tiangong-search-bindings/', {
            'server': other_server.id, 'tool': self.tool.id,
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    @patch('backend.apps.knowledge.mcp_search.call_binding')
    def test_binding_test_endpoint_updates_status(self, mock_call):
        mock_call.return_value = {
            'results': [{'url': 'https://www.example.com/a', 'title': '结果'}],
            'reason': '', 'raw_count': 1, 'elapsed_ms': 12, 'error': '',
        }
        client = APIClient()
        client.force_authenticate(user=self.admin)
        resp = client.post(
            f'/api/knowledge/tiangong-search-bindings/{self.binding.id}/test/',
            {'query': '工伤'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_status, 'ok')
        self.assertEqual(self.binding.last_result_count, 1)
        self.assertEqual(self.binding.last_latency_ms, 12)


class TiangongBingUnitTests(SimpleTestCase):
    """L3：跳转链接还原、跟踪参数清理、候选归一化与去重"""

    def test_decode_bing_redirect_payload(self):
        encoded = base64.urlsafe_b64encode(b'https://example.com/a').decode()
        href = f'https://www.bing.com/ck/a?!&&p=abc&u=a1{encoded}'
        self.assertEqual(bing._decode_bing_url(href), 'https://example.com/a')

    def test_decode_drops_bing_own_pages_and_broken_payload(self):
        self.assertEqual(bing._decode_bing_url('https://www.bing.com/search?q=x'), '')
        self.assertEqual(bing._decode_bing_url('https://www.bing.com/ck/a?u=a1!!'), '')
        self.assertEqual(bing._decode_bing_url(''), '')

    def test_clean_url_strips_tracking_and_fragment(self):
        url = 'https://example.com/a?utm_source=x&id=3&from=bing#top'
        self.assertEqual(bing._clean_url(url), 'https://example.com/a?id=3')

    def test_normalize_items_dedupes_and_filters(self):
        items = [
            {'href': 'https://example.com/a?utm_source=x', 'title': 'A', 'snippet': 's'},
            {'href': 'https://example.com/a', 'title': 'A 重复', 'snippet': ''},
            {'href': 'http://example.com/b', 'title': '非 https'},
            {'href': 'https://127.0.0.1/c', 'title': '内网'},
            {'href': 'https://www.bing.com/search?q=x', 'title': 'Bing 自身'},
        ]
        results = bing._normalize_items(items, 5)
        self.assertEqual([item['url'] for item in results], ['https://example.com/a'])
        self.assertEqual(results[0]['domain'], 'example.com')
        self.assertEqual(results[0]['engine'], 'bing')

    def test_normalize_items_respects_max_results(self):
        items = [{'href': f'https://example.com/{i}', 'title': str(i)} for i in range(5)]
        self.assertEqual(len(bing._normalize_items(items, 2)), 2)


class TiangongBingSearchTests(TestCase):
    """L3：默认关闭、缺依赖、验证码熔断、当日额度与异常降级"""

    def setUp(self):
        self._cache = dict(bing._browser_cache)
        bing._browser_cache.update({'checked_at': 0.0, 'ok': False,
                                    'reason': '', 'detail': ''})

    def tearDown(self):
        bing._browser_cache.update(self._cache)

    def _enable(self, **extra):
        SystemSetting.set_value(bing.KEY_ENABLED, 'true')
        for key, value in extra.items():
            SystemSetting.set_value(key, str(value))

    @patch('backend.apps.knowledge.bing._run_browser')
    def test_default_disabled_returns_reason(self, mock_run):
        results, reason, _elapsed = bing.search(['工伤认定'])
        self.assertEqual(results, [])
        self.assertEqual(reason, 'bing_disabled')
        mock_run.assert_not_called()

    def test_playwright_missing_is_readable_reason(self):
        with patch.object(bing, 'PLAYWRIGHT_AVAILABLE', False):
            available, reason, _detail = bing._browser_available(force=True)
        self.assertFalse(available)
        self.assertEqual(reason, 'bing_unavailable')

    @patch('backend.apps.knowledge.bing._run_browser')
    @patch('backend.apps.knowledge.bing._browser_available',
           return_value=(False, 'bing_unavailable', '未安装 playwright'))
    def test_search_skips_when_browser_unavailable(self, _avail, mock_run):
        self._enable()
        results, reason, _elapsed = bing.search(['工伤认定'])
        self.assertEqual(results, [])
        self.assertEqual(reason, 'bing_unavailable')
        self.assertEqual(SystemSetting.get_value(bing.KEY_LAST_STATUS), 'failed')
        mock_run.assert_not_called()

    @patch('backend.apps.knowledge.bing._run_browser')
    @patch('backend.apps.knowledge.bing._browser_available',
           return_value=(True, '', '/fake/chromium'))
    def test_captcha_opens_circuit_and_stops_retry(self, _avail, mock_run):
        self._enable()
        mock_run.return_value = {'results': [], 'reason': 'bing_captcha',
                                 'queries_used': 2}
        results, reason, _elapsed = bing.search(['工伤认定'])
        self.assertEqual(results, [])
        self.assertEqual(reason, 'bing_captcha')
        state = bing.circuit_state()
        self.assertTrue(state['open'])
        self.assertEqual(SystemSetting.get_value(bing.KEY_LAST_ERROR), 'bing_captcha')

        mock_run.reset_mock()
        results, reason, _elapsed = bing.search(['工伤认定'])
        self.assertEqual(reason, 'bing_circuit_open')
        mock_run.assert_not_called()

    @patch('backend.apps.knowledge.bing._run_browser')
    def test_daily_cap_blocks_before_browser(self, mock_run):
        self._enable(**{bing.KEY_DAILY_CAP: 2, bing.KEY_DAILY_DATE: bing._today(),
                        bing.KEY_DAILY_COUNT: 2})
        results, reason, _elapsed = bing.search(['工伤认定'])
        self.assertEqual(reason, 'bing_daily_limit')
        mock_run.assert_not_called()

    @patch('backend.apps.knowledge.bing._run_browser',
           side_effect=RuntimeError('launch failed'))
    @patch('backend.apps.knowledge.bing._browser_available',
           return_value=(True, '', '/fake/chromium'))
    def test_browser_exception_degrades_without_raise(self, _avail, _run):
        self._enable()
        results, reason, _elapsed = bing.search(['工伤认定'])
        self.assertEqual(results, [])
        self.assertEqual(reason, 'bing_browser_error')
        self.assertEqual(bing.circuit_state()['fail_count'], 1)

    @patch('backend.apps.knowledge.bing._run_browser')
    @patch('backend.apps.knowledge.bing._browser_available',
           return_value=(True, '', '/fake/chromium'))
    def test_success_resets_failure_and_bumps_daily(self, _avail, mock_run):
        self._enable()
        mock_run.return_value = {
            'results': [{'url': 'https://example.com/a', 'title': 'A',
                         'snippet': '摘要', 'score': 0.0,
                         'domain': 'example.com', 'engine': 'bing'}],
            'reason': '', 'queries_used': 1,
        }
        SystemSetting.set_value(bing.KEY_FAIL_COUNT, '2')
        results, reason, _elapsed = bing.search(['工伤认定'])
        self.assertEqual(reason, '')
        self.assertEqual(len(results), 1)
        self.assertEqual(bing.daily_used(), 1)
        self.assertEqual(bing.circuit_state()['fail_count'], 0)


class TiangongBingRunTests(TestCase):
    """L3 接入降级链：L1/L2 都失败才触发，结果只产候选，失败仍跑 L4"""

    def setUp(self):
        self.kb = KnowledgeBase.objects.create(
            name='工伤库', tiangong_enabled=True, tiangong_topics='工伤认定流程')
        self.run = TiangongRun.objects.create(knowledge_base=self.kb,
                                              topics='工伤认定流程', status='queued')

    def _stats(self):
        return {'created': 0, 'skipped': 0, 'pages': 0,
                'rejected': 0, 'sources': 0, 'degraded': ''}

    @patch('backend.apps.knowledge.tiangong._collect_web')
    @patch('backend.apps.knowledge.tiangong._collect_model', return_value=(0, ''))
    @patch('backend.apps.knowledge.tiangong._search_bing')
    @patch('backend.apps.knowledge.tiangong._search_searxng',
           return_value=([], 'searxng_empty', 20))
    @patch('backend.apps.knowledge.tiangong._search_mcp',
           return_value=([], 'mcp_not_configured', 0))
    def test_l3_hit_creates_bing_proposals_only(
        self, _mcp, _searxng, mock_bing, _model, mock_collect,
    ):
        mock_collect.return_value = self._stats()
        mock_bing.return_value = ([
            {'url': 'https://example.gov.cn/a', 'title': '工伤认定', 'snippet': '摘要',
             'domain': 'example.gov.cn', 'score': 0.0},
        ], '', 180)
        tiangong.execute(self.run.id)

        self.run.refresh_from_db()
        self.assertEqual(self.run.discovery_layer, 'l3_bing')
        self.assertEqual(self.run.proposals, 1)
        self.assertEqual([item['layer'] for item in self.run.fallback_chain],
                         ['l1_mcp', 'l2_searxng', 'l3_bing'])
        self.assertEqual(self.run.fallback_chain[2]['result'], 'hit')
        proposal = TiangongSourceProposal.objects.get(run=self.run)
        self.assertEqual(proposal.discovered_by, 'bing')
        self.assertEqual(proposal.status, 'pending')
        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(self.run.web_created, 0)

    @patch('backend.apps.knowledge.tiangong._collect_web')
    @patch('backend.apps.knowledge.tiangong._collect_model', return_value=(0, ''))
    @patch('backend.apps.knowledge.tiangong._search_bing',
           return_value=([], 'bing_captcha', 90))
    @patch('backend.apps.knowledge.tiangong._search_searxng',
           return_value=([], 'searxng_empty', 20))
    @patch('backend.apps.knowledge.tiangong._search_mcp',
           return_value=([], 'mcp_not_configured', 0))
    def test_l3_failure_still_runs_l4(
        self, _mcp, _searxng, _bing, _model, mock_collect,
    ):
        mock_collect.return_value = self._stats()
        tiangong.execute(self.run.id)

        self.run.refresh_from_db()
        self.assertEqual(self.run.discovery_layer, 'base')
        self.assertEqual(self.run.fallback_chain[2],
                         {'layer': 'l3_bing', 'result': 'failed',
                          'reason': 'bing_captcha', 'count': 0, 'elapsed_ms': 90})
        self.assertEqual(self.run.fallback_chain[-1]['layer'], 'l4_base')
        self.assertEqual(self.run.proposals, 0)
        mock_collect.assert_called_once()


class TiangongBingApiTests(TestCase):
    """L3：健康检查仅管理员、设置项读写与熔断重置"""

    def setUp(self):
        self.admin = User.objects.create_user(username='admin', password='x', is_staff=True)
        self.user = User.objects.create_user(username='user', password='x')

    def test_health_check_admin_only(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        self.assertEqual(client.post('/api/knowledge/tiangong-search/bing/check/').status_code,
                         403)
        client.force_authenticate(user=self.admin)
        with patch('backend.apps.knowledge.bing.health_check',
                   return_value={'ok': False, 'reason': 'bing_unavailable'}):
            resp = client.post('/api/knowledge/tiangong-search/bing/check/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['reason'], 'bing_unavailable')

    def test_settings_expose_bing_defaults(self):
        client = APIClient()
        client.force_authenticate(user=self.admin)
        with patch('backend.apps.knowledge.bing._browser_available',
                   return_value=(False, 'bing_unavailable', '')):
            resp = client.get('/api/knowledge/tiangong-search/settings/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data['bing_enabled'])
        self.assertEqual(resp.data['bing_daily_cap'], bing.DEFAULT_DAILY_CAP)
        self.assertEqual(resp.data['bing_max_queries'], bing.DEFAULT_MAX_QUERIES)
        self.assertIn('circuit', resp.data['bing_state'])
        self.assertFalse(resp.data['bing_state']['available'])

    def test_settings_update_bing_and_reset_circuit(self):
        SystemSetting.set_value(bing.KEY_OPEN_UNTIL,
                                (timezone.now() + timedelta(hours=5)).isoformat())
        client = APIClient()
        client.force_authenticate(user=self.admin)
        with patch('backend.apps.knowledge.bing._browser_available',
                   return_value=(False, 'bing_unavailable', '')):
            resp = client.post('/api/knowledge/tiangong-search/settings/', {
                'bing_enabled': True, 'bing_daily_cap': 20,
                'bing_max_queries': 2, 'reset_bing_circuit': True,
            }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data['bing_enabled'])
        self.assertEqual(resp.data['bing_daily_cap'], 20)
        self.assertEqual(resp.data['bing_max_queries'], 2)
        self.assertFalse(resp.data['bing_state']['circuit']['open'])
        self.assertTrue(AuditLog.objects.filter(
            action='tiangong_search_settings_update').exists())


