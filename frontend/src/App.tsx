import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as d3 from 'd3'
import './App.css'

type Topic = { topic: string; count: number }
type Overview = {
  guild_id: string
  total_messages: number
  active_users: number
  avg_sentiment_score: number
  top_topics: Topic[]
}

type TrendPoint = { timestamp: string; sentiment: string; score: number }
type HeatPoint = { bucket: string; count: number }
type FlowEdge = { source: string; target: string }
type GraphNode = d3.SimulationNodeDatum & { id: string }
type GraphLink = d3.SimulationLinkDatum<GraphNode> & { source: GraphNode | string; target: GraphNode | string }

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api/v1'
const DEFAULT_GUILD = import.meta.env.VITE_DEFAULT_GUILD_ID ?? 'demo-guild'

function App() {
  const [guildId, setGuildId] = useState(DEFAULT_GUILD)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [trend, setTrend] = useState<TrendPoint[]>([])
  const [heatmap, setHeatmap] = useState<HeatPoint[]>([])
  const [flowEdges, setFlowEdges] = useState<FlowEdge[]>([])
  const [error, setError] = useState('')

  const trendSvgRef = useRef<SVGSVGElement | null>(null)
  const flowSvgRef = useRef<SVGSVGElement | null>(null)

  const loadAnalytics = useCallback(async () => {
    setError('')
    try {
      const [overviewRes, trendRes, heatRes, flowRes] = await Promise.all([
        fetch(`${API_BASE}/analytics/overview?guild_id=${encodeURIComponent(guildId)}`),
        fetch(`${API_BASE}/analytics/sentiment-trend?guild_id=${encodeURIComponent(guildId)}`),
        fetch(`${API_BASE}/analytics/activity-heatmap?guild_id=${encodeURIComponent(guildId)}`),
        fetch(`${API_BASE}/analytics/interaction-flow?guild_id=${encodeURIComponent(guildId)}`),
      ])

      if (!overviewRes.ok || !trendRes.ok || !heatRes.ok || !flowRes.ok) {
        throw new Error('Analytics fetch failed. Ensure the server has opt-in and message data.')
      }

      const overviewData = (await overviewRes.json()) as Overview
      const trendData = (await trendRes.json()) as TrendPoint[]
      const heatData = (await heatRes.json()) as HeatPoint[]
      const flowData = (await flowRes.json()) as { edges: FlowEdge[] }

      setOverview(overviewData)
      setTrend(trendData)
      setHeatmap(heatData)
      setFlowEdges(flowData.edges)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unexpected analytics error')
      setOverview(null)
      setTrend([])
      setHeatmap([])
      setFlowEdges([])
    }
  }, [guildId])

  useEffect(() => {
    void loadAnalytics()
  }, [loadAnalytics])

  useEffect(() => {
    if (!trendSvgRef.current) return
    const svg = d3.select(trendSvgRef.current)
    svg.selectAll('*').remove()

    const width = 560
    const height = 220
    const margin = { top: 16, right: 16, bottom: 30, left: 40 }

    svg.attr('viewBox', `0 0 ${width} ${height}`)

    if (!trend.length) {
      svg
        .append('text')
        .attr('x', width / 2)
        .attr('y', height / 2)
        .attr('text-anchor', 'middle')
        .text('No sentiment trend data yet')
      return
    }

    const parsed = trend.map((d) => ({ ...d, date: new Date(d.timestamp) }))
    const x = d3
      .scaleTime()
      .domain(d3.extent(parsed, (d) => d.date) as [Date, Date])
      .range([margin.left, width - margin.right])

    const y = d3.scaleLinear().domain([-3, 3]).range([height - margin.bottom, margin.top])

    const line = d3
      .line<(typeof parsed)[number]>()
      .x((d) => x(d.date))
      .y((d) => y(d.score))
      .curve(d3.curveMonotoneX)

    svg
      .append('path')
      .datum(parsed)
      .attr('fill', 'none')
      .attr('stroke', '#4f46e5')
      .attr('stroke-width', 2.5)
      .attr('d', line)

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
      svg
        .append('text')
        .attr('x', width / 2)
        .attr('y', height / 2)
        .attr('text-anchor', 'middle')
        .text('No interaction graph yet')
      return
    }

    const nodes: GraphNode[] = Array.from(new Set(flowEdges.flatMap((edge) => [edge.source, edge.target]))).map((id) => ({ id }))
    const linksData: GraphLink[] = flowEdges.map((edge) => ({ source: edge.source, target: edge.target }))
    const simulation = d3
      .forceSimulation(nodes)
      .force('link', d3.forceLink<GraphNode, GraphLink>(linksData).id((d) => d.id).distance(70))
      .force('charge', d3.forceManyBody().strength(-120))
      .force('center', d3.forceCenter(width / 2, height / 2))

    const links = svg
      .append('g')
      .selectAll('line')
      .data(linksData)
      .enter()
      .append('line')
      .attr('stroke', '#9ca3af')
      .attr('stroke-opacity', 0.6)

    const circles = svg
      .append('g')
      .selectAll('circle')
      .data(nodes)
      .enter()
      .append('circle')
      .attr('r', 8)
      .attr('fill', '#2563eb')

    simulation.on('tick', () => {
      links
        .attr('x1', (d) => ((d.source as GraphNode).x ?? 0))
        .attr('y1', (d) => ((d.source as GraphNode).y ?? 0))
        .attr('x2', (d) => ((d.target as GraphNode).x ?? 0))
        .attr('y2', (d) => ((d.target as GraphNode).y ?? 0))

      circles.attr('cx', (d) => d.x ?? 0).attr('cy', (d) => d.y ?? 0)
    })

    return () => {
      simulation.stop()
    }
  }, [flowEdges])

  const topTopics = useMemo(() => overview?.top_topics ?? [], [overview])

  return (
    <div className="layout">
      <header>
        <h1>Discord Community Analytics & Research</h1>
        <p>Opt-in, privacy-safe communication insights for server administrators and academic researchers.</p>
      </header>

      <section className="controls">
        <label htmlFor="guildId">Guild ID</label>
        <input id="guildId" value={guildId} onChange={(e) => setGuildId(e.target.value)} />
        <button onClick={() => void loadAnalytics()}>Refresh</button>
      </section>

      {error ? <p className="error">{error}</p> : null}

      <section className="metrics">
        <article>
          <h2>Total Messages</h2>
          <strong>{overview?.total_messages ?? 0}</strong>
        </article>
        <article>
          <h2>Active Users</h2>
          <strong>{overview?.active_users ?? 0}</strong>
        </article>
        <article>
          <h2>Avg Sentiment</h2>
          <strong>{overview?.avg_sentiment_score ?? 0}</strong>
        </article>
      </section>

      <section className="chart-grid">
        <article>
          <h3>Sentiment Trend</h3>
          <svg ref={trendSvgRef} className="chart" />
        </article>
        <article>
          <h3>Interaction Flow</h3>
          <svg ref={flowSvgRef} className="chart" />
        </article>
      </section>

      <section className="heatmap">
        <h3>Activity Heatmap</h3>
        <div className="heat-grid">
          {heatmap.map((cell) => (
            <div key={cell.bucket} className="heat-cell" style={{ opacity: Math.min(1, 0.15 + cell.count / 12) }}>
              <span>{cell.bucket}</span>
              <strong>{cell.count}</strong>
            </div>
          ))}
          {!heatmap.length ? <p>No activity heatmap data yet</p> : null}
        </div>
      </section>

      <section>
        <h3>Top Topics</h3>
        <ul className="topics">
          {topTopics.map((topic) => (
            <li key={topic.topic}>
              <span>{topic.topic}</span>
              <strong>{topic.count}</strong>
            </li>
          ))}
          {!topTopics.length ? <li>No topic model output yet</li> : null}
        </ul>
      </section>
    </div>
  )
}

export default App
