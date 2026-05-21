import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './styles.css';

class RootErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <main
          style={{
            minHeight: '100vh',
            display: 'grid',
            placeItems: 'center',
            padding: 32,
            background: '#080d14',
            color: '#f7f8fb',
            fontFamily: 'Inter, "Microsoft YaHei", system-ui, sans-serif'
          }}
        >
          <section
            style={{
              width: 'min(520px, 100%)',
              border: '1px solid rgba(255,255,255,0.12)',
              borderRadius: 12,
              padding: 24,
              background: '#101821'
            }}
          >
            <h1 style={{ margin: '0 0 8px', fontSize: 20 }}>EcoreX Agent 界面异常</h1>
            <p style={{ margin: '0 0 16px', color: '#a8b0bd', lineHeight: 1.6 }}>
              当前页面遇到渲染错误，刷新窗口即可恢复。若问题持续出现，请在诊断页面导出运行信息。
            </p>
            <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', color: '#ff7a1a', fontSize: 12 }}>
              {this.state.error?.message || 'Unknown render error'}
            </pre>
            <button
              type="button"
              onClick={() => window.location.reload()}
              style={{
                marginTop: 16,
                height: 36,
                padding: '0 16px',
                border: 0,
                borderRadius: 8,
                background: '#ff5a00',
                color: '#fff',
                fontWeight: 700
              }}
            >
              刷新应用
            </button>
          </section>
        </main>
      );
    }

    return this.props.children;
  }
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <RootErrorBoundary>
      <App />
    </RootErrorBoundary>
  </React.StrictMode>
);
