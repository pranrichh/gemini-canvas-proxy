import React, { useState, useEffect, useRef, useCallback } from 'react';
import { createRoot } from 'react-dom/client';

// ═══════════════════════════════════════════════════════════════
// CANVAS AUTO-INJECT — DO NOT modify this line.
// ═══════════════════════════════════════════════════════════════
const apiKey = "";

const GeminiProxy = () => {
  const [status, setStatus] = useState('waiting');
  const [logs, setLogs] = useState([]);
  const [stats, setStats] = useState({ total: 0, success: 0, errors: 0 });
  const logEndRef = useRef(null);
  const seenRequests = useRef({});

  const addLog = useCallback((indicator, text, type = 'info') => {
    const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    setLogs(prev => [...prev, { time, indicator, text, type, id: Math.random() }].slice(-150));
    if (type === 'success') setStats(s => ({ ...s, total: s.total + 1, success: s.success + 1 }));
    else if (type === 'error') setStats(s => ({ ...s, total: s.total + 1, errors: s.errors + 1 }));
  }, []);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  useEffect(() => {
    const timer = setTimeout(() => {
      window.parent.postMessage({ source: 'gemini-proxy-ready' }, '*');
      setStatus('online');
      addLog('info', 'Proxy ready — waiting for requests', 'info');
    }, 500);
    return () => clearTimeout(timer);
  }, [addLog]);

  useEffect(() => {
    const handleMessage = async (event) => {
      const data = event.data;
      if (!data || data.source !== 'gemini-proxy-request') return;
      if (seenRequests.current[data.id]) return;
      seenRequests.current[data.id] = Date.now();

      const { id, method = 'POST', path, body, headers } = data;
      addLog('request', `${method} ${path.replace('/v1beta/models/', '').replace(':generateContent', '')}`);

      let url = `https://generativelanguage.googleapis.com${path}`;
      if (apiKey) url += (url.includes('?') ? '&' : '?') + 'key=' + encodeURIComponent(apiKey);

      try {
        const response = await fetch(url, {
          method,
          headers: { 'Content-Type': 'application/json', ...headers },
          body: body ? JSON.stringify(body) : undefined
        });
        
        addLog(response.ok ? 'success' : 'error', `${response.status} ${response.statusText || (response.ok ? 'OK' : 'Error')}`, response.ok ? 'success' : 'error');
        const respData = await response.json().catch(() => ({}));
        
        window.parent.postMessage({
          source: 'gemini-proxy-response',
          id,
          status: response.status,
          data: respData,
          error: response.ok ? null : `API ${response.status}`
        }, '*');
      } catch (err) {
        addLog('error', err.message, 'error');
        window.parent.postMessage({ source: 'gemini-proxy-response', id, error: err.message }, '*');
      }
    };

    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [addLog]);

  const sc = {
    waiting: { label: 'Connecting...', badge: 'WAITING', color: '#f59e0b' },
    online: { label: 'Proxy Active', badge: 'LIVE', color: '#10b981' },
    offline: { label: 'Disconnected', badge: 'OFFLINE', color: '#ef4444' }
  }[status];

  return (
    <div style={{
      fontFamily: 'Inter, system-ui, sans-serif',
      background: '#080a0f',
      color: '#e8edf5',
      minHeight: '100vh',
      padding: '32px 20px',
      maxWidth: '580px',
      margin: '0 auto'
    }}>
      <style>{`
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        .log-container::-webkit-scrollbar { width: 5px; }
        .log-container::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
      `}</style>
      
      <div style={{ marginBottom: '28px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          <div style={{ width: '40px', height: '40px', borderRadius: '12px', background: 'linear-gradient(135deg, #6366f1, #818cf8)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '18px', boxShadow: '0 0 20px rgba(99, 102, 241, 0.2)' }}>⚡</div>
          <h1 style={{ fontSize: '1.2rem', fontWeight: 700 }}>Gemini Canvas Proxy</h1>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '14px 18px', background: '#0f1218', border: '1px solid rgba(255,255,255,0.06)', borderRadius: '14px', marginBottom: '16px' }}>
        <div style={{ width: '9px', height: '9px', borderRadius: '50%', background: sc.color, boxShadow: `0 0 8px ${sc.color}`, animation: status === 'waiting' ? 'pulse 1.5s infinite' : 'none' }}></div>
        <span style={{ fontSize: '0.82rem', fontWeight: 600 }}>{sc.label}</span>
        <span style={{ marginLeft: 'auto', fontSize: '0.65rem', fontWeight: 500, padding: '3px 8px', borderRadius: '6px', background: 'rgba(255,255,255,0.05)', color: sc.color }}>{sc.badge}</span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '12px', marginBottom: '16px' }}>
        {[ ['Requests', stats.total, '#6366f1'], ['Success', stats.success, '#10b981'], ['Errors', stats.errors, '#ef4444'] ].map(([label, val, color]) => (
          <div key={label} style={{ background: '#0f1218', border: '1px solid rgba(255,255,255,0.06)', borderRadius: '10px', padding: '16px 14px', textAlign: 'center' }}>
            <div style={{ fontSize: '1.6rem', fontWeight: 700, fontFamily: 'monospace', color }}>{val}</div>
            <div style={{ fontSize: '0.62rem', color: '#4a5568', textTransform: 'uppercase', marginTop: '6px' }}>{label}</div>
          </div>
        ))}
      </div>

      <div style={{ background: '#0f1218', border: '1px solid rgba(255,255,255,0.06)', borderRadius: '14px', overflow: 'hidden' }}>
        <div style={{ padding: '12px 18px', borderBottom: '1px solid rgba(255,255,255,0.06)', display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ fontSize: '0.72rem', fontWeight: 600, color: '#7a8599', textTransform: 'uppercase' }}>Activity Log</span>
          <span style={{ fontSize: '0.65rem', color: '#4a5568', fontFamily: 'monospace' }}>{logs.length} entries</span>
        </div>
        <div className="log-container" style={{ maxHeight: '300px', overflowY: 'auto', padding: '8px 0' }}>
          {logs.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '48px 20px' }}>
              <div style={{ fontSize: '2rem', marginBottom: '12px', opacity: 0.4 }}>📡</div>
              <div style={{ fontSize: '0.78rem', color: '#7a8599' }}>Waiting for requests</div>
            </div>
          ) : logs.map(log => (
            <div key={log.id} style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '6px 18px', fontFamily: 'monospace', fontSize: '0.7rem' }}>
              <span style={{ color: '#4a5568', minWidth: '52px' }}>{log.time}</span>
              <div style={{ width: '5px', height: '5px', borderRadius: '50%', background: log.indicator === 'request' ? '#6366f1' : (log.indicator === 'success' ? '#10b981' : '#ef4444') }}></div>
              <span style={{ color: log.type === 'success' ? '#10b981' : (log.type === 'error' ? '#ef4444' : '#e8edf5') }}>{log.text}</span>
            </div>
          ))}
          <div ref={logEndRef} />
        </div>
      </div>
      <div style={{ marginTop: '20px', textAlign: 'center', fontSize: '0.62rem', color: '#4a5568' }}>
        gemini-canvas-proxy · postMessage bridge · no LNA issues
      </div>
    </div>
  );
};

// Mount
const container = document.getElementById('root') || document.body;
createRoot(container).render(<GeminiProxy />);
