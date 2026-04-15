import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as d3 from 'd3'
import './App.css'

/* ---------- types ---------- */
type Topic = { topic: string; count: number }
type Overview = { guild_id: string; total_messages: number; active_users: number; avg_sentiment_score: number; top_topics: Topic[] }
type TrendPoint = { timestamp: string; sentiment: string; score: number }
type HeatPoint = { bucket: string; count: number }
type FlowEdge = { source: string; target: string }
type GraphNode = d3.SimulationNodeDatum & { id: string }
type GraphLink = d3.SimulationLinkDatum<GraphNode> & { source: GraphNode | string; target: GraphNode | string }

type TokenRecord = { id: number; label: string; token_preview: string; source_identity?: string | null; proxy_preview?: string | null; is_active: boolean; health_status: string; rotation_priority: number; usage_count: number }
type ServerConnection = { id: number; guild_id: string; guild_name: string; role: string; enabled: boolean; joined_status: string; research_scope: string }
type ChannelMapping = { id: number; source_guild_id: string; source_channel_id: string; target_guild_id: string; target_channel_id: string; enabled: boolean; filters: Record<string, unknown>; settings: Record<string, unknown> }
type ReplicationRun = { session_id: number; status: string; generated_messages: Array<{ turn: number; account_label: string; content: string; context_aware: boolean; response_time_ms: number }> }
type QueueItem = { id: number; session_id: number; source_channel_id: string; target_channel_id: string; status: string; attempts: number; error?: string | null }
type MirrorItem = { id: number; session_id: number; source_channel_id: string; target_channel_id: string; source_content: string; replicated_content: string; source_author_hash: string; responder_account_label: string; response_time_ms: number }
type SystemStatus = { active_tokens: number; healthy_tokens: number; source_connections: number; target_connections: number; enabled_channel_mappings: number; queue_pending: number; queue_failed: number; sessions_completed: number }
type ActivityLog = { timestamp: string; event_type: string; details: Record<string, unknown> }
type ReplicationConfigSnapshot = { educational_replication_only: boolean; discord_api_base_url: string; discord_requests_per_minute: number; analytics_cache_ttl_seconds: number; openrouter_model: string }
type DashboardStats = { active_accounts: number; healthy_accounts: number; total_proxies: number; healthy_proxies: number; active_syncs: number; messages_transferred: number; ai_requests_total: number; uptime_seconds: number }
type ProxyRecord = { id: number; host: string; port: number; username: string; is_healthy: boolean; last_used: string | null; success_rate: number }
type ProxyHealth = { total: number; healthy: number; unhealthy: number; proxies: ProxyRecord[] }

type Tab = 'overview' | 'accounts' | 'proxies' | 'servers' | 'ai' | 'sync' | 'activity' | 'settings'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api/v1'
const DEFAULT_GUILD = import.meta.env.VITE_DEFAULT_GUILD_ID ?? 'demo-guild'

const TABS: { key: Tab; label: string; icon: string }[] = [
  { key: 'overview', label: 'Overview', icon: '📊' },
  { key: 'accounts', label: 'Accounts', icon: '👤' },
  { key: 'proxies', label: 'Proxies', icon: '🔄' },
  { key: 'servers', label: 'Servers', icon: '🖥️' },
  { key: 'ai', label: 'AI Config', icon: '🤖' },
  { key: 'sync', label: 'Sync', icon: '🔗' },
  { key: 'activity', label: 'Activity', icon: '📋' },
  { key: 'settings', label: 'Settings', icon: '⚙️' },
]

function App() {
  const [activeTab, setActiveTab] = useState<Tab>('overview')
  const [guildId, setGuildId] = useState(DEFAULT_GUILD)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [trend, setTrend] = useState<TrendPoint[]>([])
  const [heatmap, setHeatmap] = useState<HeatPoint[]>([])
  const [flowEdges, setFlowEdges] = useState<FlowEdge[]>([])
  const [dashStats, setDashStats] = useState<DashboardStats | null>(null)
  const [proxyHealth, setProxyHealth] = useState<ProxyHealth | null>(null)

  const [tokenLabel, setTokenLabel] = useState('token-1')
  const [tokenValue, setTokenValue] = useState('')
  const [tokenProxy, setTokenProxy] = useState('')
  const [tokenPriority, setTokenPriority] = useState(100)
  const [tokens, setTokens] = useState<TokenRecord[]>([])

  const [sourceGuildId, setSourceGuildId] = useState(DEFAULT_GUILD)
  const [targetGuildId, setTargetGuildId] = useState('1425152532807684167')
  const [connections, setConnections] = useState<ServerConnection[]>([])
  const [inviteLink, setInviteLink] = useState('')

  const [sourceChannelId, setSourceChannelId] = useState('851143244779487302')
  const [targetChannelId, setTargetChannelId] = useState('1459350794649342185')
  const [mappings, setMappings] = useState<ChannelMapping[]>([])

  const [turnCount, setTurnCount] = useState(8)
  const [contextTagTrigger, setContextTagTrigger] = useState('@')
  const [patternMinMessages, setPatternMinMessages] = useState(2)
  const [patternMaxPatterns, setPatternMaxPatterns] = useState(40)
  const [replicationRun, setReplicationRun] = useState<ReplicationRun | null>(null)
  const [queueItems, setQueueItems] = useState<QueueItem[]>([])
  const [mirrorEvents, setMirrorEvents] = useState<MirrorItem[]>([])
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null)
  const [activityLogs, setActivityLogs] = useState<ActivityLog[]>([])
  const [configSnapshot, setConfigSnapshot] = useState<ReplicationConfigSnapshot | null>(null)
  const [error, setError] = useState('')
  const [successMsg, setSuccessMsg] = useState('')

  const [aiMessage, setAiMessage] = useState('')
  const [aiResponse, setAiResponse] = useState('')
  const [aiLoading, setAiLoading] = useState(false)
  const [logFilter, setLogFilter] = useState('')

  const trendSvgRef = useRef<SVGSVGElement | null>(null)
  const flowSvgRef = useRef<SVGSVGElement | null>(null)

  /* ---------- data loaders ---------- */
  const loadAnalytics = useCallback(async () => {
    const [overviewRes, trendRes, heatRes, flowRes] = await Promise.all([
      fetch(`${API_BASE}/analytics/overview?guild_id=${encodeURIComponent(guildId)}`),
      fetch(`${API_BASE}/analytics/sentiment-trend?guild_id=${encodeURIComponent(guildId)}`),
      fetch(`${API_BASE}/analytics/activity-heatmap?guild_id=${encodeURIComponent(guildId)}`),
      fetch(`${API_BASE}/analytics/interaction-flow?guild_id=${encodeURIComponent(guildId)}`),
    ])
    if (!overviewRes.ok || !trendRes.ok || !heatRes.ok || !flowRes.ok) throw new Error('Analytics fetch failed.')
    setOverview((await overviewRes.json()) as Overview)
    setTrend((await trendRes.json()) as TrendPoint[])
    setHeatmap((await heatRes.json()) as HeatPoint[])
    setFlowEdges(((await flowRes.json()) as { edges: FlowEdge[] }).edges)
  }, [guildId])

  const loadReplicationData = useCallback(async () => {
    const [tokenRes, serverRes, mappingRes, queueRes, mirrorRes, statusRes, logsRes, configRes] = await Promise.all([
      fetch(`${API_BASE}/replication/tokens`),
      fetch(`${API_BASE}/replication/servers`),
      fetch(`${API_BASE}/replication/channel-mappings`),
      fetch(`${API_BASE}/replication/control/queue`),
      fetch(`${API_BASE}/replication/control/conversations`),
      fetch(`${API_BASE}/replication/status`),
      fetch(`${API_BASE}/replication/logs?limit=80`),
      fetch(`${API_BASE}/replication/config`),
    ])
    if (tokenRes.ok) setTokens((await tokenRes.json()) as TokenRecord[])
    if (serverRes.ok) setConnections((await serverRes.json()) as ServerConnection[])
    if (mappingRes.ok) setMappings((await mappingRes.json()) as ChannelMapping[])
    if (queueRes.ok) setQueueItems((await queueRes.json()) as QueueItem[])
    if (mirrorRes.ok) setMirrorEvents((await mirrorRes.json()) as MirrorItem[])
    if (statusRes.ok) setSystemStatus((await statusRes.json()) as SystemStatus)
    if (logsRes.ok) setActivityLogs((await logsRes.json()) as ActivityLog[])
    if (configRes.ok) setConfigSnapshot((await configRes.json()) as ReplicationConfigSnapshot)
  }, [])

  const loadDashboard = useCallback(async () => {
    const [statsRes, proxyRes] = await Promise.all([
      fetch(`${API_BASE}/dashboard/stats`),
      fetch(`${API_BASE}/proxies/health`),
    ])
    if (statsRes.ok) setDashStats((await statsRes.json()) as DashboardStats)
    if (proxyRes.ok) setProxyHealth((await proxyRes.json()) as ProxyHealth)
  }, [])

  const loadAll = useCallback(async () => {
    setError('')
    try {
      await Promise.all([loadAnalytics(), loadReplicationData(), loadDashboard()])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected error while loading data')
    }
  }, [loadAnalytics, loadReplicationData, loadDashboard])

  useEffect(() => { void loadAll() }, [loadAll])

  /* ---------- D3 charts ---------- */
  useEffect(() => {
    if (!trendSvgRef.current) return
    const svg = d3.select(trendSvgRef.current)
    svg.selectAll('*').remove()
    const width = 560, height = 220, margin = { top: 16, right: 16, bottom: 30, left: 40 }
    svg.attr('viewBox', `0 0 ${width} ${height}`)
    if (!trend.length) { svg.append('text').attr('x', width / 2).attr('y', height / 2).attr('text-anchor', 'middle').attr('fill', '#9ca3af').text('No sentiment trend data yet'); return }
    const parsed = trend.map((d) => ({ ...d, date: new Date(d.timestamp) }))
    const x = d3.scaleTime().domain(d3.extent(parsed, (d) => d.date) as [Date, Date]).range([margin.left, width - margin.right])
    const y = d3.scaleLinear().domain([-3, 3]).range([height - margin.bottom, margin.top])
    const line = d3.line<(typeof parsed)[number]>().x((d) => x(d.date)).y((d) => y(d.score)).curve(d3.curveMonotoneX)
    svg.append('path').datum(parsed).attr('fill', 'none').attr('stroke', '#7c3aed').attr('stroke-width', 2.5).attr('d', line)
    svg.append('g').attr('transform', `translate(0,${height - margin.bottom})`).call(d3.axisBottom(x).ticks(5))
    svg.append('g').attr('transform', `translate(${margin.left},0)`).call(d3.axisLeft(y).ticks(5))
  }, [trend])

  useEffect(() => {
    if (!flowSvgRef.current) return
    const svg = d3.select(flowSvgRef.current)
    svg.selectAll('*').remove()
    const width = 560, height = 260
    svg.attr('viewBox', `0 0 ${width} ${height}`)
    if (!flowEdges.length) { svg.append('text').attr('x', width / 2).attr('y', height / 2).attr('text-anchor', 'middle').attr('fill', '#9ca3af').text('No interaction graph yet'); return }
    const nodes: GraphNode[] = Array.from(new Set(flowEdges.flatMap((edge) => [edge.source, edge.target]))).map((id) => ({ id }))
    const linksData: GraphLink[] = flowEdges.map((edge) => ({ source: edge.source, target: edge.target }))
    const simulation = d3.forceSimulation(nodes).force('link', d3.forceLink<GraphNode, GraphLink>(linksData).id((d) => d.id).distance(70)).force('charge', d3.forceManyBody().strength(-120)).force('center', d3.forceCenter(width / 2, height / 2))
    const links = svg.append('g').selectAll('line').data(linksData).enter().append('line').attr('stroke', '#6b7280').attr('stroke-opacity', 0.6)
    const circles = svg.append('g').selectAll('circle').data(nodes).enter().append('circle').attr('r', 8).attr('fill', '#7c3aed')
    simulation.on('tick', () => {
      links.attr('x1', (d) => ((d.source as GraphNode).x ?? 0)).attr('y1', (d) => ((d.source as GraphNode).y ?? 0)).attr('x2', (d) => ((d.target as GraphNode).x ?? 0)).attr('y2', (d) => ((d.target as GraphNode).y ?? 0))
      circles.attr('cx', (d) => d.x ?? 0).attr('cy', (d) => d.y ?? 0)
    })
    return () => { simulation.stop() }
  }, [flowEdges])

  /* ---------- actions ---------- */
  const flash = (msg: string) => { setSuccessMsg(msg); setTimeout(() => setSuccessMsg(''), 3000) }

  const submitToken = async () => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/tokens`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label: tokenLabel, token_value: tokenValue, proxy_value: tokenProxy || undefined, rotation_priority: tokenPriority }) })
      if (!response.ok) throw new Error('Failed to save token')
      setTokenValue(''); setTokenProxy('')
      await loadReplicationData()
      flash('Token saved successfully')
    } catch (err) { setError(err instanceof Error ? err.message : 'Unexpected token error') }
  }

  const addServerConnection = async (role: 'source' | 'target') => {
    const guild_id = role === 'source' ? sourceGuildId : targetGuildId
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/servers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ guild_id, guild_name: guild_id, role, enabled: true }) })
      if (!response.ok) throw new Error(`Failed to save ${role} server`)
      await loadReplicationData()
      flash(`${role} server connected`)
    } catch (err) { setError(err instanceof Error ? err.message : 'Unexpected server connection error') }
  }

  const addChannelMapping = async () => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/channel-mappings`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_guild_id: sourceGuildId, source_channel_id: sourceChannelId, target_guild_id: targetGuildId, target_channel_id: targetChannelId, enabled: true, settings: {}, filters: {} }) })
      if (!response.ok) throw new Error('Failed to save channel mapping')
      await loadReplicationData()
      flash('Channel mapping saved')
    } catch (err) { setError(err instanceof Error ? err.message : 'Unexpected channel mapping error') }
  }

  const capturePatterns = async () => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/patterns/capture`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_guild_id: sourceGuildId, min_messages_per_user: patternMinMessages, max_patterns: patternMaxPatterns }) })
      if (!response.ok) throw new Error('Failed to capture message patterns')
      flash('Patterns captured')
    } catch (err) { setError(err instanceof Error ? err.message : 'Unexpected pattern capture error') }
  }

  const runReplication = async () => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/control/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_guild_id: sourceGuildId, target_guild_id: targetGuildId, turn_count: turnCount, context_tag_trigger: contextTagTrigger, educational_mode_confirmed: true }) })
      if (!response.ok) throw new Error('Failed to start replication run')
      setReplicationRun((await response.json()) as ReplicationRun)
      await loadReplicationData()
      flash('Replication completed')
    } catch (err) { setError(err instanceof Error ? err.message : 'Unexpected replication error') }
  }

  const runHealthCheck = async (tokenId: number) => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/tokens/${tokenId}/health-check`, { method: 'POST' })
      if (!response.ok) throw new Error('Token health check failed')
      await loadReplicationData()
      flash('Health check complete')
    } catch (err) { setError(err instanceof Error ? err.message : 'Unexpected token health error') }
  }

  const toggleToken = async (tokenId: number, active: boolean) => {
    try {
      await fetch(`${API_BASE}/replication/tokens/${tokenId}/status`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ is_active: !active }) })
      await loadReplicationData()
    } catch { /* ignore */ }
  }

  const loadTokensFromFile = async () => {
    setError('')
    try {
      const res = await fetch(`${API_BASE}/accounts/load-file`, { method: 'POST' })
      if (!res.ok) throw new Error('Failed to load tokens from t.txt')
      const data = await res.json() as { loaded: number; errors: string[] }
      await loadReplicationData()
      flash(`Loaded ${data.loaded} token(s) from t.txt`)
    } catch (err) { setError(err instanceof Error ? err.message : 'Load error') }
  }

  const loadProxiesFromFile = async () => {
    setError('')
    try {
      const res = await fetch(`${API_BASE}/proxies/load-file`, { method: 'POST' })
      if (!res.ok) throw new Error('Failed to load proxies from p.txt')
      const data = await res.json() as { loaded: number; errors: string[] }
      await Promise.all([loadReplicationData(), loadDashboard()])
      flash(`Loaded ${data.loaded} proxy/proxies from p.txt`)
    } catch (err) { setError(err instanceof Error ? err.message : 'Load error') }
  }

  const loadApiConfig = async () => {
    setError('')
    try {
      const res = await fetch(`${API_BASE}/config/load-file`, { method: 'POST' })
      if (!res.ok) throw new Error('Failed to load api_key.conf')
      flash('API config loaded from api_key.conf')
    } catch (err) { setError(err instanceof Error ? err.message : 'Load error') }
  }

  const sendAiMessage = async () => {
    if (!aiMessage.trim()) return
    setAiLoading(true); setAiResponse(''); setError('')
    try {
      const res = await fetch(`${API_BASE}/ai/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: aiMessage }) })
      if (!res.ok) throw new Error('AI chat request failed')
      const data = await res.json() as { response: string; model: string }
      setAiResponse(data.response)
    } catch (err) { setError(err instanceof Error ? err.message : 'AI error') }
    setAiLoading(false)
  }

  const copyInviteLink = () => {
    if (!inviteLink) {
      flash('Enter an invite link first')
      return
    }
    void navigator.clipboard.writeText(inviteLink)
    flash('Invite link copied to clipboard!')
  }

  const joinWithOnboarding = async (guildId: string, invite: string) => {
    if (!invite) { setError('Enter an invite link first'); return }
    setError('')
    try {
      const code = invite.trim().replace(/\/$/, '').split('/').pop() ?? invite
      const res = await fetch(
        `${API_BASE}/replication/servers/join-with-onboarding?guild_id=${encodeURIComponent(guildId)}&invite_code=${encodeURIComponent(code)}`,
        { method: 'POST' },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Unknown error' }))
        throw new Error(typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail))
      }
      const data = await res.json() as { results: Array<{ label: string; status: string }> }
      const summary = data.results.map(r => `${r.label}: ${r.status}`).join(', ')
      await loadReplicationData()
      flash(`Join results — ${summary}`)
    } catch (err) { setError(err instanceof Error ? err.message : 'Join error') }
  }

  const topTopics = useMemo(() => overview?.top_topics ?? [], [overview])
  const filteredLogs = useMemo(() => {
    if (!logFilter) return activityLogs
    const lower = logFilter.toLowerCase()
    return activityLogs.filter(l => l.event_type.toLowerCase().includes(lower) || JSON.stringify(l.details).toLowerCase().includes(lower))
  }, [activityLogs, logFilter])
  const formatUptime = (s: number) => { const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60); return `${h}h ${m}m` }

  const healthDot = (status: string) => {
    const colors: Record<string, string> = { healthy: '#22c55e', unknown: '#facc15', invalid: '#ef4444', unreachable: '#ef4444' }
    return <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: '50%', background: colors[status] ?? '#9ca3af', marginRight: 6 }} />
  }

  /* ---------- render ---------- */
  return (
    <div className="app-shell">
      {/* Sidebar */}
      <nav className="sidebar">
        <div className="sidebar-brand">
          <span className="brand-icon">⚔️</span>
          <h1>Friend Army</h1>
        </div>
        <ul className="nav-list">
          {TABS.map((tab) => (
            <li key={tab.key}>
              <button className={`nav-btn${activeTab === tab.key ? ' active' : ''}`} onClick={() => setActiveTab(tab.key)}>
                <span className="nav-icon">{tab.icon}</span>{tab.label}
              </button>
            </li>
          ))}
        </ul>
        <div className="sidebar-footer">
          <button className="btn-sm btn-outline" onClick={() => void loadAll()}>🔄 Refresh All</button>
        </div>
      </nav>

      {/* Main content */}
      <main className="main-content">
        <header className="top-bar">
          <h2>{TABS.find(t => t.key === activeTab)?.icon} {TABS.find(t => t.key === activeTab)?.label}</h2>
          <div className="top-bar-right">
            <span className="status-pill">{dashStats ? `${dashStats.active_accounts} accounts online` : '...'}</span>
            {dashStats ? <span className="uptime-badge">⏱ {formatUptime(dashStats.uptime_seconds)}</span> : null}
          </div>
        </header>

        {error && <div className="toast toast-error">{error} <button className="toast-close" onClick={() => setError('')}>×</button></div>}
        {successMsg && <div className="toast toast-success">{successMsg}</div>}

        {/* ===== OVERVIEW TAB ===== */}
        {activeTab === 'overview' && (
          <div className="tab-content">
            <div className="stat-cards">
              <div className="stat-card purple"><div className="stat-number">{dashStats?.active_accounts ?? 0}</div><div className="stat-label">Active Accounts</div></div>
              <div className="stat-card green"><div className="stat-number">{dashStats?.healthy_accounts ?? 0}</div><div className="stat-label">Healthy Accounts</div></div>
              <div className="stat-card blue"><div className="stat-number">{dashStats?.total_proxies ?? 0}</div><div className="stat-label">Total Proxies</div></div>
              <div className="stat-card cyan"><div className="stat-number">{dashStats?.healthy_proxies ?? 0}</div><div className="stat-label">Healthy Proxies</div></div>
              <div className="stat-card orange"><div className="stat-number">{dashStats?.active_syncs ?? 0}</div><div className="stat-label">Active Syncs</div></div>
              <div className="stat-card pink"><div className="stat-number">{dashStats?.messages_transferred ?? 0}</div><div className="stat-label">Messages Transferred</div></div>
            </div>

            <div className="panel-row">
              <div className="panel controls-row">
                <label>Analytics Guild ID</label>
                <input value={guildId} onChange={(e) => setGuildId(e.target.value)} />
                <button className="btn-primary" onClick={() => void loadAll()}>Refresh</button>
              </div>
            </div>

            <div className="panel-row">
              <section className="panel chart-panel"><h3>📈 Sentiment Trend</h3><svg ref={trendSvgRef} className="chart" /></section>
              <section className="panel chart-panel"><h3>🔗 Interaction Flow</h3><svg ref={flowSvgRef} className="chart" /></section>
            </div>

            <section className="panel">
              <h3>🔥 Activity Heatmap</h3>
              <div className="heat-grid">
                {heatmap.map((cell) => (<div key={cell.bucket} className="heat-cell" style={{ opacity: Math.min(1, 0.15 + cell.count / 12) }}><span>{cell.bucket}</span><strong>{cell.count}</strong></div>))}
                {!heatmap.length && <p className="empty-text">No activity heatmap data yet</p>}
              </div>
            </section>

            <section className="panel">
              <h3>🏆 Top Topics</h3>
              <div className="topic-chips">
                {topTopics.map((topic) => (<span key={topic.topic} className="chip">{topic.topic} <strong>{topic.count}</strong></span>))}
                {!topTopics.length && <p className="empty-text">No topic model output yet</p>}
              </div>
            </section>

            <section className="panel">
              <h3>⚡ System Status</h3>
              <div className="stat-cards mini">
                <div className="stat-card-mini"><strong>{systemStatus?.active_tokens ?? 0}</strong><span>Active Tokens</span></div>
                <div className="stat-card-mini"><strong>{systemStatus?.healthy_tokens ?? 0}</strong><span>Healthy Tokens</span></div>
                <div className="stat-card-mini"><strong>{systemStatus?.enabled_channel_mappings ?? 0}</strong><span>Mappings</span></div>
                <div className="stat-card-mini"><strong>{systemStatus?.queue_pending ?? 0}</strong><span>Queue Pending</span></div>
                <div className="stat-card-mini"><strong>{systemStatus?.queue_failed ?? 0}</strong><span>Queue Failed</span></div>
                <div className="stat-card-mini"><strong>{systemStatus?.sessions_completed ?? 0}</strong><span>Sessions Done</span></div>
              </div>
            </section>
          </div>
        )}

        {/* ===== ACCOUNTS TAB ===== */}
        {activeTab === 'accounts' && (
          <div className="tab-content">
            <section className="panel">
              <div className="panel-header">
                <h3>👤 Add Account Token</h3>
                <button className="btn-secondary" onClick={() => void loadTokensFromFile()}>📁 Load from t.txt</button>
              </div>
              <div className="form-grid-2col">
                <div className="form-group"><label>Label</label><input value={tokenLabel} onChange={(e) => setTokenLabel(e.target.value)} /></div>
                <div className="form-group"><label>Priority</label><input type="number" value={tokenPriority} onChange={(e) => setTokenPriority(Number(e.target.value))} min={1} max={1000} /></div>
                <div className="form-group full-width"><label>Discord Token</label><input type="password" placeholder="Paste Discord token here" value={tokenValue} onChange={(e) => setTokenValue(e.target.value)} /></div>
                <div className="form-group full-width"><label>Proxy (optional)</label><input type="password" placeholder="host:port:username:password" value={tokenProxy} onChange={(e) => setTokenProxy(e.target.value)} /></div>
              </div>
              <button className="btn-primary" onClick={() => void submitToken()}>💾 Save Token</button>
            </section>

            <section className="panel">
              <h3>📋 Loaded Accounts ({tokens.length})</h3>
              {!tokens.length && <p className="empty-text">No account tokens configured. Add one above or load from t.txt.</p>}
              <div className="token-list">
                {tokens.map((token) => (
                  <div key={token.id} className="token-card">
                    <div className="token-card-header">
                      {healthDot(token.health_status)}
                      <strong>{token.label}</strong>
                      <span className="token-preview">{token.token_preview}</span>
                    </div>
                    <div className="token-card-meta">
                      <span>Identity: {token.source_identity ?? 'token-only'}</span>
                      <span>Proxy: {token.proxy_preview ?? 'none'}</span>
                      <span>Usage: {token.usage_count} | Priority: {token.rotation_priority}</span>
                    </div>
                    <div className="token-card-actions">
                      <button className="btn-xs" onClick={() => void runHealthCheck(token.id)}>🔍 Health Check</button>
                      <button className="btn-xs btn-outline" onClick={() => void toggleToken(token.id, token.is_active)}>{token.is_active ? '⏸ Disable' : '▶ Enable'}</button>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}

        {/* ===== PROXIES TAB ===== */}
        {activeTab === 'proxies' && (
          <div className="tab-content">
            <section className="panel">
              <div className="panel-header">
                <h3>🔄 Proxy Management</h3>
                <button className="btn-secondary" onClick={() => void loadProxiesFromFile()}>📁 Load from p.txt</button>
              </div>
              <div className="stat-cards mini">
                <div className="stat-card-mini green-bg"><strong>{proxyHealth?.healthy ?? 0}</strong><span>Healthy</span></div>
                <div className="stat-card-mini red-bg"><strong>{proxyHealth?.unhealthy ?? 0}</strong><span>Unhealthy</span></div>
                <div className="stat-card-mini"><strong>{proxyHealth?.total ?? 0}</strong><span>Total</span></div>
              </div>
            </section>

            <section className="panel">
              <h3>📋 Proxy List</h3>
              {!proxyHealth?.proxies.length && <p className="empty-text">No proxies loaded. Use &quot;Load from p.txt&quot; to import proxies.</p>}
              <div className="proxy-list">
                {proxyHealth?.proxies.map((proxy) => (
                  <div key={proxy.id} className="proxy-card">
                    <div className="proxy-info">
                      <span className={`status-dot ${proxy.is_healthy ? 'green' : 'red'}`} />
                      <strong>{proxy.host}:{proxy.port}</strong>
                      <span className="proxy-user">@{proxy.username}</span>
                    </div>
                    <div className="proxy-stats">
                      <span>Success: {proxy.success_rate}%</span>
                      {proxy.last_used && <span>Last: {new Date(proxy.last_used).toLocaleString()}</span>}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          </div>
        )}

        {/* ===== SERVERS TAB ===== */}
        {activeTab === 'servers' && (
          <div className="tab-content">
            <section className="panel">
              <h3>🖥️ Target Server Configuration</h3>
              <div className="form-grid-2col">
                <div className="form-group"><label>Source Guild ID</label><input value={sourceGuildId} onChange={(e) => setSourceGuildId(e.target.value)} /></div>
                <div className="form-group"><button className="btn-primary full-height" onClick={() => void addServerConnection('source')}>Connect Source</button></div>
                <div className="form-group"><label>Target Guild ID</label><input value={targetGuildId} onChange={(e) => setTargetGuildId(e.target.value)} /></div>
                <div className="form-group"><button className="btn-primary full-height" onClick={() => void addServerConnection('target')}>Connect Target</button></div>
              </div>
            </section>

            <section className="panel">
              <h3>🔗 Invite Link</h3>
              <div className="invite-row">
                <input placeholder="Paste invite link here" value={inviteLink} onChange={(e) => setInviteLink(e.target.value)} />
                <button className="btn-secondary" onClick={copyInviteLink}>📋 Copy Link</button>
              </div>
            </section>

            <section className="panel">
              <h3>🚪 Join Servers (with Onboarding Auto-Complete)</h3>
              <p className="panel-desc" style={{ marginBottom: 12, color: '#9ca3af', fontSize: 13 }}>
                Sends all active account tokens to join a server via invite. If the server has Discord Onboarding enabled,
                each token will automatically complete the onboarding prompts so it can send messages right away.
              </p>
              <div className="form-grid-2col">
                <div className="form-group">
                  <label>Base / Source Server</label>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <input readOnly value="https://discord.gg/ttzewo" style={{ flex: 1 }} />
                    <button className="btn-secondary" onClick={() => void joinWithOnboarding(sourceGuildId, 'https://discord.gg/ttzewo')}>Join All Tokens</button>
                  </div>
                </div>
                <div className="form-group">
                  <label>Target Server</label>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <input readOnly value="https://discord.gg/asTTvgMe" style={{ flex: 1 }} />
                    <button className="btn-secondary" onClick={() => void joinWithOnboarding(targetGuildId, 'https://discord.gg/asTTvgMe')}>Join All Tokens</button>
                  </div>
                </div>
                <div className="form-group full-width">
                  <label>Custom Invite (for any other server)</label>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <input placeholder="https://discord.gg/…" value={inviteLink} onChange={(e) => setInviteLink(e.target.value)} style={{ flex: 1 }} />
                    <button className="btn-primary" onClick={() => void joinWithOnboarding(sourceGuildId, inviteLink)}>Join All Tokens</button>
                  </div>
                </div>
              </div>
            </section>

            <section className="panel">
              <h3>📡 Channel Mapping</h3>
              <div className="form-grid-2col">
                <div className="form-group"><label>Source Channel ID</label><input value={sourceChannelId} onChange={(e) => setSourceChannelId(e.target.value)} /></div>
                <div className="form-group"><label>Target Channel ID</label><input value={targetChannelId} onChange={(e) => setTargetChannelId(e.target.value)} /></div>
              </div>
              <button className="btn-primary" onClick={() => void addChannelMapping()}>💾 Save Channel Mapping</button>
            </section>

            <section className="panel">
              <h3>Active Connections ({connections.length})</h3>
              <div className="connection-list">
                {connections.map((c) => (
                  <div key={c.id} className="connection-card">
                    <div className="connection-header">
                      {healthDot(c.joined_status === 'joined' ? 'healthy' : 'unknown')}
                      <strong>{c.guild_name}</strong>
                      <span className={`role-badge ${c.role}`}>{c.role}</span>
                    </div>
                    <span className="connection-meta">{c.joined_status} • {c.research_scope}</span>
                  </div>
                ))}
                {!connections.length && <p className="empty-text">No server connections configured.</p>}
              </div>
            </section>

            <section className="panel">
              <h3>Channel Mappings ({mappings.length})</h3>
              <div className="mapping-list">
                {mappings.map((m) => (
                  <div key={m.id} className="mapping-card">
                    <strong>{m.source_channel_id}</strong> → <strong>{m.target_channel_id}</strong>
                    <span className="mapping-meta">{m.source_guild_id} → {m.target_guild_id}</span>
                  </div>
                ))}
                {!mappings.length && <p className="empty-text">No channel mappings configured.</p>}
              </div>
            </section>
          </div>
        )}

        {/* ===== AI CONFIG TAB ===== */}
        {activeTab === 'ai' && (
          <div className="tab-content">
            <section className="panel">
              <h3>🤖 AI Configuration (OpenRouter / Grok-4.1-fast)</h3>
              <div className="config-info">
                <div className="config-item"><label>Model</label><strong>{configSnapshot?.openrouter_model ?? 'x-ai/grok-4.1-fast'}</strong></div>
                <div className="config-item"><label>API Key</label><strong>••••••••••</strong></div>
              </div>
              <button className="btn-secondary" onClick={() => void loadApiConfig()}>📁 Reload api_key.conf</button>
            </section>

            <section className="panel">
              <h3>💬 Test AI Chat</h3>
              <div className="ai-chat-area">
                <textarea placeholder="Type your message to AI..." value={aiMessage} onChange={(e) => setAiMessage(e.target.value)} rows={3} />
                <button className="btn-primary" onClick={() => void sendAiMessage()} disabled={aiLoading}>{aiLoading ? '⏳ Generating...' : '🚀 Send'}</button>
              </div>
              {aiResponse && (
                <div className="ai-response-box">
                  <h4>AI Response:</h4>
                  <p>{aiResponse}</p>
                </div>
              )}
            </section>
          </div>
        )}

        {/* ===== SYNC TAB ===== */}
        {activeTab === 'sync' && (
          <div className="tab-content">
            <section className="panel">
              <h3>🔗 Conversation Sync Controls</h3>
              <div className="form-grid-2col">
                <div className="form-group"><label>Min Messages/User</label><input type="number" min={1} max={1000} value={patternMinMessages} onChange={(e) => setPatternMinMessages(Number(e.target.value))} /></div>
                <div className="form-group"><label>Max Patterns</label><input type="number" min={1} max={200} value={patternMaxPatterns} onChange={(e) => setPatternMaxPatterns(Number(e.target.value))} /></div>
                <div className="form-group"><label>Turn Count</label><input type="number" min={1} max={200} value={turnCount} onChange={(e) => setTurnCount(Number(e.target.value))} /></div>
                <div className="form-group"><label>Context Trigger</label><input value={contextTagTrigger} onChange={(e) => setContextTagTrigger(e.target.value)} /></div>
              </div>
              <div className="btn-row">
                <button className="btn-secondary" onClick={() => void capturePatterns()}>📸 Capture Patterns</button>
                <button className="btn-primary" onClick={() => void runReplication()}>▶️ Run Sync</button>
              </div>
            </section>

            <section className="panel">
              <h3>📨 Latest Replication Turns</h3>
              <div className="mirror-list">
                {replicationRun?.generated_messages.map((msg) => (
                  <div key={`${msg.turn}-${msg.account_label}`} className="mirror-card">
                    <div className="mirror-header"><strong>Turn {msg.turn}</strong><span className="badge">{msg.context_aware ? 'mention-driven' : 'sample-driven'}</span><span>{msg.response_time_ms}ms</span></div>
                    <p><strong>{msg.account_label}:</strong> {msg.content}</p>
                  </div>
                ))}
                {!replicationRun && <p className="empty-text">No replication run executed in this session.</p>}
              </div>
            </section>

            <section className="panel">
              <h3>🪞 Source vs Replicated</h3>
              <div className="mirror-list">
                {mirrorEvents.slice(0, 10).map((item) => (
                  <div key={item.id} className="mirror-card">
                    <div className="mirror-header"><strong>{item.source_channel_id} → {item.target_channel_id}</strong><span>{item.response_time_ms}ms</span></div>
                    <p className="source-text">Source: {item.source_content}</p>
                    <p className="replica-text">Replica: {item.replicated_content}</p>
                    <span className="mirror-meta">{item.responder_account_label}</span>
                  </div>
                ))}
                {!mirrorEvents.length && <p className="empty-text">No mirrored conversation events yet.</p>}
              </div>
            </section>

            <section className="panel">
              <h3>📦 Queue ({queueItems.length})</h3>
              <div className="queue-list">
                {queueItems.slice(0, 10).map((item) => (
                  <div key={item.id} className="queue-card">
                    <strong>#{item.id}</strong>
                    <span>{item.source_channel_id} → {item.target_channel_id}</span>
                    <span className={`status-badge ${item.status}`}>{item.status}</span>
                    <span>attempts: {item.attempts}</span>
                  </div>
                ))}
                {!queueItems.length && <p className="empty-text">No queue activity yet.</p>}
              </div>
            </section>
          </div>
        )}

        {/* ===== ACTIVITY TAB ===== */}
        {activeTab === 'activity' && (
          <div className="tab-content">
            <section className="panel">
              <div className="panel-header">
                <h3>📋 Activity Monitor</h3>
                <input className="search-input" placeholder="Filter logs..." value={logFilter} onChange={(e) => setLogFilter(e.target.value)} />
              </div>
              <div className="log-list">
                {filteredLogs.slice(0, 30).map((item, i) => (
                  <div key={`${item.timestamp}-${item.event_type}-${i}`} className="log-entry">
                    <span className="log-time">{new Date(item.timestamp).toLocaleString()}</span>
                    <strong className="log-type">{item.event_type}</strong>
                    <span className="log-details">{JSON.stringify(item.details)}</span>
                  </div>
                ))}
                {!filteredLogs.length && <p className="empty-text">No activity logs yet.</p>}
              </div>
            </section>
          </div>
        )}

        {/* ===== SETTINGS TAB ===== */}
        {activeTab === 'settings' && (
          <div className="tab-content">
            <section className="panel">
              <h3>⚙️ Configuration Snapshot</h3>
              <div className="config-list">
                <div className="config-row"><span>Educational-only mode</span><strong>{configSnapshot?.educational_replication_only ? '✅ Enabled' : '❌ Disabled'}</strong></div>
                <div className="config-row"><span>Discord API base URL</span><strong>{configSnapshot?.discord_api_base_url ?? '-'}</strong></div>
                <div className="config-row"><span>Discord RPM limit</span><strong>{configSnapshot?.discord_requests_per_minute ?? 0}</strong></div>
                <div className="config-row"><span>Cache TTL (seconds)</span><strong>{configSnapshot?.analytics_cache_ttl_seconds ?? 0}</strong></div>
                <div className="config-row"><span>OpenRouter model</span><strong>{configSnapshot?.openrouter_model ?? 'x-ai/grok-4.1-fast'}</strong></div>
              </div>
            </section>

            <section className="panel">
              <h3>📁 Reload Credentials</h3>
              <div className="btn-row">
                <button className="btn-secondary" onClick={() => void loadTokensFromFile()}>📄 Reload t.txt (Tokens)</button>
                <button className="btn-secondary" onClick={() => void loadProxiesFromFile()}>📄 Reload p.txt (Proxies)</button>
                <button className="btn-secondary" onClick={() => void loadApiConfig()}>📄 Reload api_key.conf</button>
              </div>
            </section>
          </div>
        )}
      </main>
    </div>
  )
}

export default App
