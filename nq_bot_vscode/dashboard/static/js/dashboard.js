/**
 * NQ Trading Bot — Real-Time Monitoring Dashboard
 * WebSocket + Chart.js + DOM updates
 */

(function () {
    'use strict';

    // ═══ Auth ═══
    const token = localStorage.getItem('dashboard_token');
    if (!token) {
        window.location.href = '/login';
        return;
    }
    const authHeaders = { 'Authorization': 'Bearer ' + token };

    // ═══ State ═══
    let ws = null;
    let reconnectDelay = 1000;
    const MAX_RECONNECT = 16000;
    const priceData = [];
    const vwapData = [];
    const entryMarkers = [];
    const exitMarkers = [];
    const equityData = [];
    const slippageData = [];

    // ═══ Chart.js Defaults ═══
    Chart.defaults.color = '#a0a0b0';
    Chart.defaults.borderColor = 'rgba(42,42,78,0.5)';
    Chart.defaults.font.family = "'Segoe UI', sans-serif";

    // ═══ Price Chart ═══
    const priceCtx = document.getElementById('priceChart').getContext('2d');
    const priceChart = new Chart(priceCtx, {
        type: 'line',
        data: {
            datasets: [
                {
                    label: 'Price',
                    data: priceData,
                    borderColor: '#4488ff',
                    backgroundColor: 'rgba(68,136,255,0.05)',
                    borderWidth: 1.5,
                    pointRadius: 0,
                    tension: 0.1,
                    fill: true,
                },
                {
                    label: 'VWAP',
                    data: vwapData,
                    borderColor: '#ffaa00',
                    borderWidth: 1,
                    pointRadius: 0,
                    borderDash: [4, 4],
                    tension: 0.1,
                    fill: false,
                },
                {
                    label: 'Entry',
                    data: entryMarkers,
                    borderColor: '#00ff88',
                    backgroundColor: '#00ff88',
                    pointRadius: 6,
                    pointStyle: 'triangle',
                    showLine: false,
                },
                {
                    label: 'Exit',
                    data: exitMarkers,
                    borderColor: '#ff4444',
                    backgroundColor: '#ff4444',
                    pointRadius: 6,
                    pointStyle: 'rectRot',
                    showLine: false,
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'index' },
            scales: {
                x: {
                    type: 'time',
                    time: { unit: 'minute', displayFormats: { minute: 'HH:mm' } },
                    grid: { display: false },
                },
                y: {
                    position: 'right',
                    grid: { color: 'rgba(42,42,78,0.3)' },
                }
            },
            plugins: {
                legend: { display: true, position: 'top', labels: { boxWidth: 12, padding: 10 } },
            },
            animation: { duration: 0 },
        }
    });

    // ═══ Equity Chart ═══
    const equityCtx = document.getElementById('equityChart').getContext('2d');
    const equityChart = new Chart(equityCtx, {
        type: 'line',
        data: {
            datasets: [{
                label: 'Equity',
                data: equityData,
                borderColor: '#00ff88',
                backgroundColor: 'rgba(0,255,136,0.08)',
                borderWidth: 1.5,
                pointRadius: 0,
                fill: true,
                tension: 0.2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: { type: 'time', time: { unit: 'day' }, grid: { display: false } },
                y: { position: 'right', grid: { color: 'rgba(42,42,78,0.3)' } }
            },
            plugins: { legend: { display: false } },
            animation: { duration: 0 },
        }
    });

    // ═══ Slippage Chart ═══
    const slipCtx = document.getElementById('slippageChart').getContext('2d');
    const slippageChart = new Chart(slipCtx, {
        type: 'bar',
        data: {
            datasets: [{
                label: 'Slippage (ticks)',
                data: slippageData,
                backgroundColor: function (ctx) {
                    const v = ctx.raw ? ctx.raw.y : 0;
                    return v > 2 ? 'rgba(255,68,68,0.6)' : v > 0 ? 'rgba(255,170,0,0.6)' : 'rgba(0,255,136,0.6)';
                },
                borderWidth: 0,
                barPercentage: 0.8,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: { type: 'time', time: { unit: 'hour' }, grid: { display: false } },
                y: { position: 'right', grid: { color: 'rgba(42,42,78,0.3)' } }
            },
            plugins: { legend: { display: false } },
            animation: { duration: 0 },
        }
    });

    // ═══ WebSocket ═══
    function connectWS() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = proto + '//' + location.host + '/ws?token=' + encodeURIComponent(token);
        ws = new WebSocket(wsUrl);

        ws.onopen = function () {
            reconnectDelay = 1000;
            setConnDot('connWS', true);
            console.log('WS connected');
        };

        ws.onclose = function () {
            setConnDot('connWS', false);
            console.log('WS disconnected, reconnecting in', reconnectDelay, 'ms');
            setTimeout(connectWS, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT);
        };

        ws.onerror = function () {
            setConnDot('connWS', false);
        };

        ws.onmessage = function (evt) {
            try {
                const msg = JSON.parse(evt.data);
                handleMessage(msg);
            } catch (e) {
                console.error('WS parse error', e);
            }
        };
    }

    function handleMessage(msg) {
        switch (msg.type) {
            case 'bar':
                onBar(msg.data);
                break;
            case 'signal':
                onSignal(msg.data);
                break;
            case 'trade':
                onTrade(msg.data);
                break;
            case 'regime':
                onRegime(msg.data);
                break;
            case 'alert':
                onAlert(msg.data);
                break;
            case 'pnl':
                onPnl(msg.data);
                break;
            case 'full_state':
                onFullState(msg.data);
                break;
            case 'heartbeat':
                // keep alive
                break;
            case 'pong':
                break;
        }
    }

    // ═══ Message Handlers ═══
    function onBar(data) {
        const t = data.timestamp ? new Date(data.timestamp) : new Date();
        priceData.push({ x: t, y: data.price });
        if (priceData.length > 500) priceData.shift();

        if (data.vwap) {
            vwapData.push({ x: t, y: data.vwap });
            if (vwapData.length > 500) vwapData.shift();
        }
        priceChart.update('none');
    }

    function onSignal(data) {
        // Could update heatmap live
    }

    function onTrade(data) {
        const t = new Date();
        if (data.action === 'entry') {
            entryMarkers.push({ x: t, y: data.price });
            if (entryMarkers.length > 50) entryMarkers.shift();
        } else if (data.action === 'exit') {
            exitMarkers.push({ x: t, y: data.price });
            if (exitMarkers.length > 50) exitMarkers.shift();
        }
        priceChart.update('none');
        fetchTrades();
    }

    function onRegime(data) {
        updateRegime(data.state, data.vix);
    }

    function onAlert(data) {
        addAlertItem(data.severity, data.message, new Date().toLocaleTimeString());
    }

    function onPnl(data) {
        updatePnl(data.daily, data.unrealized);
    }

    function onFullState(data) {
        // Update everything from full state
        if (data.risk_state) {
            updatePnl(data.risk_state.daily_pnl, 0);
            if (data.risk_state.kill_switch_active) {
                setBotStatus('KILL_SWITCH');
            }
        }
        if (data.current_regime) {
            updateRegime(data.current_regime, data.risk_state ? data.risk_state.vix : null);
        }
        if (data.running !== undefined) {
            if (data.running) setBotStatus('RUNNING');
            else setBotStatus('STOPPED');
        }
        if (data.health) {
            setConnDot('connIBKR', data.health.execution === 'healthy');
            setConnDot('connDB', data.health.data === 'healthy');
            setConnDot('connFeed', data.health.features === 'healthy');
        }
        if (data.equity_curve && data.equity_curve.length) {
            equityData.length = 0;
            const now = new Date();
            data.equity_curve.forEach(function (val, i) {
                const d = new Date(now.getTime() - (data.equity_curve.length - i) * 86400000);
                equityData.push({ x: d, y: val });
            });
            equityChart.update('none');
        }
        if (data.alerts) {
            data.alerts.forEach(function (a) {
                addAlertItem(a.level || 'INFO', a.message, a.timestamp ? new Date(a.timestamp).toLocaleTimeString() : '');
            });
        }
    }

    // ═══ DOM Updaters ═══
    function setBotStatus(status) {
        const el = document.getElementById('botStatus');
        el.textContent = status;
        el.className = 'status-badge ' + status.toLowerCase().replace('_', '-');
    }

    function updatePnl(daily, unrealized) {
        const dailyEl = document.getElementById('dailyPnl');
        const unrealEl = document.getElementById('unrealizedPnl');
        dailyEl.textContent = '$' + (daily || 0).toFixed(2);
        dailyEl.className = 'value ' + ((daily || 0) >= 0 ? 'positive' : 'negative');
        unrealEl.textContent = '$' + (unrealized || 0).toFixed(2);
        unrealEl.className = 'value ' + ((unrealized || 0) >= 0 ? 'positive' : 'negative');
    }

    function updateRegime(state, vix) {
        const bar = document.getElementById('regimeBar');
        const text = document.getElementById('regimeText');
        const vixEl = document.getElementById('regimeVix');
        bar.className = 'regime-bar ' + (state || 'unknown');
        text.textContent = (state || 'Unknown').replace(/_/g, ' ').toUpperCase();
        vixEl.textContent = vix != null ? 'VIX: ' + vix.toFixed(1) : 'VIX: ---';
    }

    function setConnDot(id, ok) {
        const el = document.getElementById(id);
        if (el) el.className = 'conn-dot ' + (ok ? 'ok' : 'err');
    }

    function addAlertItem(severity, message, time) {
        const feed = document.getElementById('alertFeed');
        const li = document.createElement('li');
        li.className = 'alert-item ' + (severity || 'INFO');
        li.innerHTML = '<span class="alert-time">' + escapeHtml(time || '') + '</span>'
            + '<span class="alert-severity">' + escapeHtml(severity || '') + '</span>'
            + escapeHtml(message || '');
        feed.prepend(li);
        // Keep max 10
        while (feed.children.length > 10) feed.removeChild(feed.lastChild);
    }

    function updateTradeTable(trades) {
        const tbody = document.getElementById('tradeBody');
        tbody.innerHTML = '';
        (trades || []).slice(-20).reverse().forEach(function (t) {
            const tr = document.createElement('tr');
            const pnl = t.pnl || t.total_pnl || 0;
            const side = t.side || t.direction || '';
            const time = t.entryTime || t.entry_time || t.timestamp || '';
            const timeStr = time ? new Date(typeof time === 'number' ? time : time).toLocaleTimeString() : '';
            tr.innerHTML = '<td>' + escapeHtml(timeStr) + '</td>'
                + '<td class="' + side + '">' + escapeHtml(side.toUpperCase()) + '</td>'
                + '<td>' + (t.entryPrice || t.entry_price || '').toString() + '</td>'
                + '<td>' + (t.exitPrice || t.exit_price || '').toString() + '</td>'
                + '<td class="' + (pnl >= 0 ? 'long' : 'short') + '">$' + pnl.toFixed(2) + '</td>'
                + '<td>' + (t.signalScore || t.signal_score || 0).toFixed(2) + '</td>';
            tbody.appendChild(tr);
        });
    }

    function updateHeatmap(data) {
        const grid = document.getElementById('heatmapGrid');
        grid.innerHTML = '';
        const rows = ['Technical', 'Discord', 'ML', 'Sweep', 'HTF'];
        const keys = ['technical', 'discord', 'ml', 'sweep', 'htf'];

        rows.forEach(function (label, ri) {
            const lbl = document.createElement('div');
            lbl.className = 'heatmap-label';
            lbl.textContent = label;
            grid.appendChild(lbl);

            for (var i = 0; i < 20; i++) {
                const cell = document.createElement('div');
                cell.className = 'heatmap-cell';
                var val = 0;
                if (data && data[keys[ri]] && data[keys[ri]][i] !== undefined) {
                    val = data[keys[ri]][i];
                }
                // Color: 0=dark, 1=bright green, negative=red
                if (val > 0) {
                    const intensity = Math.min(val, 1);
                    cell.style.background = 'rgba(0,255,136,' + (intensity * 0.8 + 0.1) + ')';
                } else if (val < 0) {
                    const intensity = Math.min(Math.abs(val), 1);
                    cell.style.background = 'rgba(255,68,68,' + (intensity * 0.8 + 0.1) + ')';
                } else {
                    cell.style.background = 'rgba(42,42,78,0.3)';
                }
                grid.appendChild(cell);
            }
        });
    }

    function updateHTFBias(data) {
        const grid = document.getElementById('htfGrid');
        grid.innerHTML = '';
        const timeframes = ['1D', '4H', '1H', '30m', '15m', '5m'];

        timeframes.forEach(function (tf) {
            const item = document.createElement('div');
            item.className = 'htf-item';

            var direction = 'neutral';
            var arrow = '--';
            if (data && data[tf]) {
                direction = data[tf].direction || data[tf] || 'neutral';
            }

            if (direction === 'bullish' || direction === 'long') {
                arrow = '\u25B2'; // ▲
            } else if (direction === 'bearish' || direction === 'short') {
                arrow = '\u25BC'; // ▼
            } else {
                arrow = '\u25C6'; // ◆
            }

            var cssClass = 'neutral';
            if (direction === 'bullish' || direction === 'long') cssClass = 'bullish';
            else if (direction === 'bearish' || direction === 'short') cssClass = 'bearish';

            item.innerHTML = '<div class="tf-label">' + tf + '</div>'
                + '<div class="tf-arrow ' + cssClass + '">' + arrow + '</div>';
            grid.appendChild(item);
        });
    }

    // ═══ REST API Fetchers ═══
    function apiFetch(url) {
        return fetch(url, { headers: authHeaders }).then(function (r) {
            if (r.status === 401 || r.status === 403) {
                localStorage.removeItem('dashboard_token');
                window.location.href = '/login';
                throw new Error('Unauthorized');
            }
            return r.json();
        });
    }

    function fetchEquityCurve() {
        apiFetch('/api/equity-curve').then(function (data) {
            equityData.length = 0;
            (data.points || []).forEach(function (pt) {
                equityData.push({ x: new Date(pt.timestamp || pt.date), y: pt.equity || pt.value });
            });
            equityChart.update('none');
        }).catch(function () { });
    }

    function fetchRegime() {
        apiFetch('/api/regime').then(function (data) {
            updateRegime(data.current_state || data.state, data.vix);
        }).catch(function () { });
    }

    function fetchHeatmap() {
        apiFetch('/api/signals/heatmap').then(function (data) {
            updateHeatmap(data);
        }).catch(function () { });
    }

    function fetchExecutionQuality() {
        apiFetch('/api/execution/quality').then(function (data) {
            slippageData.length = 0;
            (data.history || []).forEach(function (pt) {
                slippageData.push({ x: new Date(pt.timestamp), y: pt.slippage_ticks || 0 });
            });
            slippageChart.update('none');
        }).catch(function () { });
    }

    function fetchAlerts() {
        apiFetch('/api/alerts/history').then(function (data) {
            (data.alerts || []).forEach(function (a) {
                addAlertItem(a.severity || a.level, a.message, a.timestamp ? new Date(a.timestamp).toLocaleTimeString() : '');
            });
        }).catch(function () { });
    }

    function fetchHTFBias() {
        apiFetch('/api/htf-bias').then(function (data) {
            updateHTFBias(data.timeframes || data);
        }).catch(function () { });
    }

    function fetchTrades() {
        apiFetch('/api/trades').then(function (data) {
            var trades = data.recent_trades || data;
            if (Array.isArray(trades)) updateTradeTable(trades);
        }).catch(function () { });
    }

    function fetchContractStatus() {
        apiFetch('/api/contract/status').then(function (data) {
            document.getElementById('contractInfo').textContent = data.symbol || '---';
            document.getElementById('rollDays').textContent = data.days_until_expiry != null ? data.days_until_expiry + 'd' : '---';
        }).catch(function () { });
    }

    // ═══ Init ═══
    function init() {
        // Initial data load
        fetchEquityCurve();
        fetchRegime();
        fetchHeatmap();
        fetchExecutionQuality();
        fetchAlerts();
        fetchHTFBias();
        fetchTrades();
        fetchContractStatus();

        // Initialize empty heatmap
        updateHeatmap(null);
        updateHTFBias(null);

        // Connect WebSocket
        connectWS();

        // Periodic refresh
        setInterval(fetchEquityCurve, 60000);
        setInterval(fetchRegime, 15000);
        setInterval(fetchHeatmap, 10000);
        setInterval(fetchExecutionQuality, 30000);
        setInterval(fetchAlerts, 30000);
        setInterval(fetchHTFBias, 30000);
        setInterval(fetchContractStatus, 300000);

        // Heartbeat ping
        setInterval(function () {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 10000);
    }

    // ═══ Helpers ═══
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // Start
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
