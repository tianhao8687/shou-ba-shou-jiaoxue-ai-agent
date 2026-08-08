import { BookMarked, Braces, Database, FileText, Search, Split, Waypoints } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { HealthResponse, KnowledgeDoc } from '../types'

export function KnowledgeView({ documents, health }: { documents: KnowledgeDoc[]; health?: HealthResponse }) {
  const [query, setQuery] = useState('')
  const filtered = useMemo(() => documents.filter((doc) => `${doc.id}${doc.title}${doc.preview}`.toLowerCase().includes(query.toLowerCase())), [documents, query])
  return (
    <div className="page-view knowledge-view">
      <header className="page-heading">
        <div><span className="section-kicker">RETRIEVAL GROUNDING</span><h1>运行手册知识库</h1><p>Agent 先检索候选手册，再用现场观测约束诊断。系统会如实暴露向量质量，不把特征哈希包装成语义 Embedding。</p></div>
        <label className="search-field"><Search size={17} /><span className="sr-only">搜索知识库</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索手册或编号" /></label>
      </header>

      <section className="retrieval-flow panel" aria-label="RAG 检索流程">
        {[
          [FileText, '文档', 'Markdown 手册'],
          [Split, '切块', '按章节保留语义'],
          [Braces, '双路索引', health?.vector_quality === 'semantic' ? 'Embedding + BM25' : '特征哈希 + BM25'],
          [Database, '存储', `${health?.vector_backend ?? '加载中'} / ${health?.database ?? '存储'}`],
          [Waypoints, '融合召回', 'RRF + 安全策略固定引用'],
          [BookMarked, '引用', '来源随答案返回'],
        ].map(([Icon, title, copy], index) => {
          const FlowIcon = Icon as typeof FileText
          return <div className="flow-item" key={title as string}><span><FlowIcon size={19} /></span><strong>{title as string}</strong><small>{copy as string}</small>{index < 5 && <i />}</div>
        })}
      </section>

      <aside className={`retrieval-disclosure ${health?.vector_quality === 'semantic' ? 'semantic' : 'baseline'}`}>
        <strong>{health?.vector_quality === 'semantic' ? '当前为真实语义向量通道' : '当前为可复现检索基线'}</strong>
        <span>{health?.vector_quality === 'semantic' ? '向量由外部 Embedding 模型生成，并与词法召回融合。' : '没有发现本地 Embedding 模型，所以使用 Feature Hashing 做相似度基线；部署真实 Embedding 后健康接口才会标记 semantic。'}</span>
        <code>{health?.vector_quality ?? 'unavailable'}</code>
      </aside>

      <div className="knowledge-grid">
        {filtered.map((doc) => (
          <article className="knowledge-card" key={doc.id}>
            <div className="doc-top"><span>{doc.id}</span><small>{doc.characters} 字符</small></div>
            <h2>{doc.title}</h2><p>{doc.preview}</p>
            <div className="doc-foot"><code>{doc.uri}</code><span>{doc.section}</span></div>
          </article>
        ))}
      </div>
      {filtered.length === 0 && <section className="empty-state inline"><Search size={22} /><h2>没有匹配手册</h2><p>换一个服务名或编号试试。</p></section>}
    </div>
  )
}
