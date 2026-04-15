import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as d3 from 'd3'
import './App.css'

type Topic = { topic: string; count: number }
type Overview = { guild_id: string; total_messages: number; active_users: number; avg_sentiment_score: number; top_topics: Topic[] }
type TrendPoint = { timestamp: string; sentiment: string; score: number }
type HeatPoint = { bucket: string; count: number }
type FlowEdge = { source: string; target: string }
type GraphNode = d3.SimulationNodeDatum & { id: string }
type GraphLink = d3.SimulationLinkDatum<GraphNode> & { source: GraphNode | string; target: GraphNode | string }

type TokenRecord = {
  id: number
  label: string
  token_preview: string
  source_identity?: string | null
  proxy_preview?: string | null
  is_active: boolean
  health_status: string
  rotation_priority: number
  usage_count: number
}
type ServerConnection = { id: number; guild_id: string; guild_name: string; role: string; enabled: boolean; joined_status: string; research_scope: string }
type ChannelMapping = { id: number; source_guild_id: string; source_channel_id: string; target_guild_id: string; target_channel_id: string; enabled: boolean; filters: Record<string, unknown>; settings: Record<string, unknown> }
type ReplicationRun = { session_id: number; status: string; generated_messages: Array<{ turn: number; account_label: string; content: string; context_aware: boolean; response_time_ms: number }> }
type QueueItem = { id: number; session_id: number; source_channel_id: string; target_channel_id: string; status: string; attempts: number; error?: string | null }
type MirrorItem = { id: number; session_id: number; source_channel_id: string; target_channel_id: string; source_content: string; replicated_content: string; source_author_hash: string; responder_account_label: string; response_time_ms: number }
type SystemStatus = { active_tokens: number; healthy_tokens: number; source_connections: number; target_connections: number; enabled_channel_mappings: number; queue_pending: number; queue_failed: number; sessions_completed: number }
type ActivityLog = { timestamp: string; event_type: string; details: Record<string, unknown> }
type ReplicationConfigSnapshot = {
  educational_replication_only: boolean
  discord_api_base_url: string
  discord_requests_per_minute: number
  analytics_cache_ttl_seconds: number
  openrouter_model: string
}

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api/v1'
const DEFAULT_GUILD = import.meta.env.VITE_DEFAULT_GUILD_ID ?? 'demo-guild'

function App() {
  const [guildId, setGuildId] = useState(DEFAULT_GUILD)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [trend, setTrend] = useState<TrendPoint[]>([])
  const [heatmap, setHeatmap] = useState<HeatPoint[]>([])
  const [flowEdges, setFlowEdges] = useState<FlowEdge[]>([])

  const [tokenLabel, setTokenLabel] = useState('research-account-1')
  const [tokenValue, setTokenValue] = useState('')
  const [tokenProxy, setTokenProxy] = useState('')
  const [tokenPriority, setTokenPriority] = useState(100)
  const [tokens, setTokens] = useState<TokenRecord[]>([])

  const [sourceGuildId, setSourceGuildId] = useState(DEFAULT_GUILD)
  const [targetGuildId, setTargetGuildId] = useState('target-guild')
  const [connections, setConnections] = useState<ServerConnection[]>([])

  const [sourceChannelId, setSourceChannelId] = useState('source-channel')
  const [targetChannelId, setTargetChannelId] = useState('target-channel')
  const [mappings, setMappings] = useState<ChannelMapping[]>([])

  const [turnCount, setTurnCount] = useState(8)
  const [contextTagTrigger, setContextTagTrigger] = useState('@')
  const [patternMinMessages, setPatternMinMessages] = useState(2)
  const [patternMaxPatterns, setPatternMaxPatterns] = useState(40)
  const [mappingPace, setMappingPace] = useState('adaptive')
  const [includeThreads, setIncludeThreads] = useState(true)
  const [replicationRun, setReplicationRun] = useState<ReplicationRun | null>(null)
  const [queueItems, setQueueItems] = useState<QueueItem[]>([])
  const [mirrorEvents, setMirrorEvents] = useState<MirrorItem[]>([])
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null)
  const [activityLogs, setActivityLogs] = useState<ActivityLog[]>([])
  const [configSnapshot, setConfigSnapshot] = useState<ReplicationConfigSnapshot | null>(null)
  const [error, setError] = useState('')

  const trendSvgRef = useRef<SVGSVGElement | null>(null)
  const flowSvgRef = useRef<SVGSVGElement | null>(null)

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

  const loadAll = useCallback(async () => {
    setError('')
    try {
      await Promise.all([loadAnalytics(), loadReplicationData()])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected error while loading data')
    }
  }, [loadAnalytics, loadReplicationData])

  useEffect(() => {
    void loadAll()
  }, [loadAll])

  useEffect(() => {
    if (!trendSvgRef.current) return
    const svg = d3.select(trendSvgRef.current)
    svg.selectAll('*').remove()
    const width = 560
    const height = 220
    const margin = { top: 16, right: 16, bottom: 30, left: 40 }
    svg.attr('viewBox', `0 0 ${width} ${height}`)
    if (!trend.length) {
      svg.append('text').attr('x', width / 2).attr('y', height / 2).attr('text-anchor', 'middle').text('No sentiment trend data yet')
      return
    }
    const parsed = trend.map((d) => ({ ...d, date: new Date(d.timestamp) }))
    const x = d3.scaleTime().domain(d3.extent(parsed, (d) => d.date) as [Date, Date]).range([margin.left, width - margin.right])
    const y = d3.scaleLinear().domain([-3, 3]).range([height - margin.bottom, margin.top])
    const line = d3.line<(typeof parsed)[number]>().x((d) => x(d.date)).y((d) => y(d.score)).curve(d3.curveMonotoneX)
    svg.append('path').datum(parsed).attr('fill', 'none').attr('stroke', '#4f46e5').attr('stroke-width', 2.5).attr('d', line)
    svg.append('g').attr('transform', `translate(0,${height - margin.bottom})`).call(d3.axisBottom(x).ticks(5))
    svg.append('g').attr('transform', `translate(${margin.left},0)`).call(d3.axisLeft(y).ticks(5))
  }, [trend])

  useEffect(() => {
    if (!flowSvgRef.current) return
    const svg = d3.select(flowSvgRef.current)
    svg.selectAll('*').remove()
    const width = 560
    const height = 260
    svg.attr('viewBox', `0 0 ${width} ${height}`)
    if (!flowEdges.length) {
      svg.append('text').attr('x', width / 2).attr('y', height / 2).attr('text-anchor', 'middle').text('No interaction graph yet')
      return
    }
    const nodes: GraphNode[] = Array.from(new Set(flowEdges.flatMap((edge) => [edge.source, edge.target]))).map((id) => ({ id }))
    const linksData: GraphLink[] = flowEdges.map((edge) => ({ source: edge.source, target: edge.target }))
    const simulation = d3
      .forceSimulation(nodes)
      .force('link', d3.forceLink<GraphNode, GraphLink>(linksData).id((d) => d.id).distance(70))
      .force('charge', d3.forceManyBody().strength(-120))
      .force('center', d3.forceCenter(width / 2, height / 2))
    const links = svg.append('g').selectAll('line').data(linksData).enter().append('line').attr('stroke', '#9ca3af').attr('stroke-opacity', 0.6)
    const circles = svg.append('g').selectAll('circle').data(nodes).enter().append('circle').attr('r', 8).attr('fill', '#2563eb')
    simulation.on('tick', () => {
      links.attr('x1', (d) => ((d.source as GraphNode).x ?? 0)).attr('y1', (d) => ((d.source as GraphNode).y ?? 0)).attr('x2', (d) => ((d.target as GraphNode).x ?? 0)).attr('y2', (d) => ((d.target as GraphNode).y ?? 0))
      circles.attr('cx', (d) => d.x ?? 0).attr('cy', (d) => d.y ?? 0)
    })
    return () => {
      simulation.stop()
    }
  }, [flowEdges])

  const submitToken = async () => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/tokens`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label: tokenLabel, token_value: tokenValue, proxy_value: tokenProxy, rotation_priority: tokenPriority }) })
      if (!response.ok) throw new Error('Failed to save token')
      setTokenValue('')
      setTokenProxy('')
      await loadReplicationData()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected token error')
    }
  }

  const addServerConnection = async (role: 'source' | 'target') => {
    const guild_id = role === 'source' ? sourceGuildId : targetGuildId
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/servers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ guild_id, guild_name: guild_id, role, enabled: true }) })
      if (!response.ok) throw new Error(`Failed to save ${role} server`)
      await loadReplicationData()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected server connection error')
    }
  }

  const addChannelMapping = async () => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/channel-mappings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_guild_id: sourceGuildId,
          source_channel_id: sourceChannelId,
          target_guild_id: targetGuildId,
          target_channel_id: targetChannelId,
          enabled: true,
          settings: { pace: mappingPace },
          filters: { include_threads: includeThreads },
        }),
      })
      if (!response.ok) throw new Error('Failed to save channel mapping')
      await loadReplicationData()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected channel mapping error')
    }
  }

  const capturePatterns = async () => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/patterns/capture`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_guild_id: sourceGuildId, min_messages_per_user: patternMinMessages, max_patterns: patternMaxPatterns }) })
      if (!response.ok) throw new Error('Failed to capture message patterns')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected pattern capture error')
    }
  }

  const runReplication = async () => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/control/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_guild_id: sourceGuildId,
          target_guild_id: targetGuildId,
          turn_count: turnCount,
          context_tag_trigger: contextTagTrigger,
          educational_mode_confirmed: true,
        }),
      })
      if (!response.ok) throw new Error('Failed to start replication run')
      setReplicationRun((await response.json()) as ReplicationRun)
      await loadReplicationData()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected replication error')
    }
  }

  const runHealthCheck = async (tokenId: number) => {
    setError('')
    try {
      const response = await fetch(`${API_BASE}/replication/tokens/${tokenId}/health-check`, { method: 'POST' })
      if (!response.ok) throw new Error('Token health check failed')
      await loadReplicationData()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected token health error')
    }
  }

  const topTopics = useMemo(() => overview?.top_topics ?? [], [overview])

  return (
    <div className="layout">
      <header>
        <h1>Discord Analytics + Educational Conversation Replication</h1>
        <p>Controlled-environment tooling for studying communication patterns with anonymization safeguards.</p>
      </header>

      <section className="panel controls">
        <label htmlFor="guildId">Analytics Guild ID</label>
        <input id="guildId" value={guildId} onChange={(e) => setGuildId(e.target.value)} />
        <button onClick={() => void loadAll()}>Refresh</button>
      </section>

      {error ? <p className="error">{error}</p> : null}

      <section className="metrics">
        <article><h2>Total Messages</h2><strong>{overview?.total_messages ?? 0}</strong></article>
        <article><h2>Active Users</h2><strong>{overview?.active_users ?? 0}</strong></article>
        <article><h2>Avg Sentiment</h2><strong>{overview?.avg_sentiment_score ?? 0}</strong></article>
      </section>

      <section className="chart-grid">
        <article className="panel"><h3>Sentiment Trend</h3><svg ref={trendSvgRef} className="chart" /></article>
        <article className="panel"><h3>Interaction Flow</h3><svg ref={flowSvgRef} className="chart" /></article>
      </section>

      <section className="panel heatmap">
        <h3>Activity Heatmap</h3>
        <div className="heat-grid">
          {heatmap.map((cell) => (
            <div key={cell.bucket} className="heat-cell" style={{ opacity: Math.min(1, 0.15 + cell.count / 12) }}><span>{cell.bucket}</span><strong>{cell.count}</strong></div>
          ))}
          {!heatmap.length ? <p>No activity heatmap data yet</p> : null}
        </div>
      </section>

      <section className="panel status-grid">
        <h3>System Status</h3>
        <div className="metrics status-metrics">
          <article><h2>Active Tokens</h2><strong>{systemStatus?.active_tokens ?? 0}</strong></article>
          <article><h2>Healthy Tokens</h2><strong>{systemStatus?.healthy_tokens ?? 0}</strong></article>
          <article><h2>Mappings</h2><strong>{systemStatus?.enabled_channel_mappings ?? 0}</strong></article>
          <article><h2>Queue Pending</h2><strong>{systemStatus?.queue_pending ?? 0}</strong></article>
          <article><h2>Queue Failed</h2><strong>{systemStatus?.queue_failed ?? 0}</strong></article>
          <article><h2>Completed Sessions</h2><strong>{systemStatus?.sessions_completed ?? 0}</strong></article>
        </div>
      </section>

      <section className="panel split-grid">
        <div>
          <h3>Account Token Management</h3>
          <div className="form-grid">
            <input placeholder="Token label" value={tokenLabel} onChange={(e) => setTokenLabel(e.target.value)} />
             <input placeholder="Discord user token" value={tokenValue} onChange={(e) => setTokenValue(e.target.value)} />
            <input placeholder="Proxy host:port:user:pass (optional)" value={tokenProxy} onChange={(e) => setTokenProxy(e.target.value)} />
            <input type="number" value={tokenPriority} onChange={(e) => setTokenPriority(Number(e.target.value))} min={1} max={1000} />
            <button onClick={() => void submitToken()}>Save Token</button>
          </div>
          <ul className="collection">
            {tokens.map((token) => (
              <li key={token.id}><div><strong>{token.label}</strong><p>{token.token_preview}</p><small>{token.source_identity ?? 'token-only'} • {token.proxy_preview ?? 'no-proxy'}</small><small>{token.health_status} • usage {token.usage_count}</small></div><button onClick={() => void runHealthCheck(token.id)}>Health Check</button></li>
            ))}
            {!tokens.length ? <li>No account tokens configured.</li> : null}
          </ul>
        </div>

        <div>
          <h3>Server + Channel Configuration</h3>
          <div className="form-grid">
            <input placeholder="Source guild ID" value={sourceGuildId} onChange={(e) => setSourceGuildId(e.target.value)} />
            <button onClick={() => void addServerConnection('source')}>Connect Source</button>
            <input placeholder="Target guild ID" value={targetGuildId} onChange={(e) => setTargetGuildId(e.target.value)} />
            <button onClick={() => void addServerConnection('target')}>Connect Target</button>
            <input placeholder="Source channel ID" value={sourceChannelId} onChange={(e) => setSourceChannelId(e.target.value)} />
            <input placeholder="Target channel ID" value={targetChannelId} onChange={(e) => setTargetChannelId(e.target.value)} />
            <input placeholder="Mapping pace (adaptive/slow/fast)" value={mappingPace} onChange={(e) => setMappingPace(e.target.value)} />
            <label><input type="checkbox" checked={includeThreads} onChange={(e) => setIncludeThreads(e.target.checked)} /> Include threads</label>
            <button onClick={() => void addChannelMapping()}>Save Channel Mapping</button>
          </div>
        </div>
      </section>

      <section className="panel split-grid">
        <div>
          <h3>Replication Controls</h3>
          <div className="form-grid">
            <div className="inline-controls"><input type="number" min={1} max={1000} value={patternMinMessages} onChange={(e) => setPatternMinMessages(Number(e.target.value))} /><input type="number" min={1} max={200} value={patternMaxPatterns} onChange={(e) => setPatternMaxPatterns(Number(e.target.value))} /><button onClick={() => void capturePatterns()}>Capture Patterns</button></div>
            <div className="inline-controls"><input type="number" min={1} max={200} value={turnCount} onChange={(e) => setTurnCount(Number(e.target.value))} /><input value={contextTagTrigger} onChange={(e) => setContextTagTrigger(e.target.value)} /><button onClick={() => void runReplication()}>Run Replication</button></div>
          </div>
          <ul className="collection">
            {queueItems.slice(0, 8).map((item) => (
              <li key={item.id}><div><strong>Queue #{item.id}</strong><p>{item.source_channel_id} → {item.target_channel_id}</p><small>{item.status} • attempts {item.attempts}</small></div></li>
            ))}
            {!queueItems.length ? <li>No queue activity yet.</li> : null}
          </ul>
        </div>
        <div>
          <h3>Mappings & Connections</h3>
          <ul className="collection">
            {connections.map((connection) => (
              <li key={connection.id}><div><strong>{connection.guild_name}</strong><p>{connection.role} • {connection.joined_status}</p></div></li>
            ))}
            {!connections.length ? <li>No server connections configured.</li> : null}
          </ul>
          <ul className="collection">
            {mappings.map((mapping) => (
              <li key={mapping.id}><div><strong>{mapping.source_channel_id} → {mapping.target_channel_id}</strong><p>{mapping.source_guild_id} → {mapping.target_guild_id}</p></div></li>
            ))}
            {!mappings.length ? <li>No channel mappings configured.</li> : null}
          </ul>
        </div>
      </section>

      <section className="panel split-grid">
        <div>
          <h3>Latest Replication Turns</h3>
          <ul className="collection">
            {replicationRun?.generated_messages.map((message) => (
              <li key={`${message.turn}-${message.account_label}`}><div><strong>Turn {message.turn}</strong><p>{message.account_label}: {message.content}</p><small>{message.context_aware ? 'mention-driven response' : 'sample-driven response'} • {message.response_time_ms} ms</small></div></li>
            ))}
            {!replicationRun ? <li>No replication run executed in this session.</li> : null}
          </ul>
        </div>

        <div>
          <h3>Source vs Replicated Conversation View</h3>
          <ul className="collection">
            {mirrorEvents.slice(0, 10).map((item) => (
              <li key={item.id}><div><strong>{item.source_channel_id} → {item.target_channel_id}</strong><p>Source: {item.source_content}</p><p>Replica: {item.replicated_content}</p><small>{item.responder_account_label} • {item.response_time_ms} ms</small></div></li>
            ))}
            {!mirrorEvents.length ? <li>No mirrored conversation events yet.</li> : null}
          </ul>
        </div>
      </section>

      <section className="panel">
        <h3>Replication Configuration Snapshot</h3>
        <ul className="topics">
          <li><span>Educational-only mode</span><strong>{configSnapshot?.educational_replication_only ? 'enabled' : 'disabled'}</strong></li>
          <li><span>Discord API base URL</span><strong>{configSnapshot?.discord_api_base_url ?? '-'}</strong></li>
          <li><span>Discord RPM limit</span><strong>{configSnapshot?.discord_requests_per_minute ?? 0}</strong></li>
          <li><span>Cache TTL (seconds)</span><strong>{configSnapshot?.analytics_cache_ttl_seconds ?? 0}</strong></li>
          <li><span>OpenRouter model</span><strong>{configSnapshot?.openrouter_model ?? '-'}</strong></li>
        </ul>
      </section>

      <section className="panel">
        <h3>Activity Logs</h3>
        <ul className="collection">
          {activityLogs.slice(0, 20).map((item) => (
            <li key={`${item.timestamp}-${item.event_type}`}><div><strong>{item.event_type}</strong><p>{item.timestamp}</p><small>{JSON.stringify(item.details)}</small></div></li>
          ))}
          {!activityLogs.length ? <li>No activity logs yet.</li> : null}
        </ul>
      </section>

      <section className="panel">
        <h3>Top Topics</h3>
        <ul className="topics">
          {topTopics.map((topic) => (<li key={topic.topic}><span>{topic.topic}</span><strong>{topic.count}</strong></li>))}
          {!topTopics.length ? <li>No topic model output yet</li> : null}
        </ul>
      </section>
    </div>
  )
}

export default App
