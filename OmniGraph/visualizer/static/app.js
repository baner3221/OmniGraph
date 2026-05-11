/**
 * OmniGraph Visualizer — Cytoscape.js Graph Engine
 *
 * Interactive knowledge graph explorer with:
 * - On-demand subgraph loading from Neo4j
 * - Call chain and inheritance tree views
 * - Edge type filtering
 * - Configurable layouts (dagre, cose, circle)
 * - Click-to-expand exploration
 *
 * Libraries: Cytoscape.js (MIT), dagre (MIT), cytoscape-dagre (MIT)
 */

// ---- State ----
const state = {
    cy: null,
    currentView: 'namespace',
    activeEdges: new Set(['CALLS', 'INHERITS_FROM', 'DEFINES', 'OVERRIDES']),
    currentLayout: 'dagre',
    selectedNode: null,
    depth: 3,
    searchTimeout: null,
};

// ---- Edge colors ----
const EDGE_COLORS = {
    CALLS: '#3b82f6',
    INHERITS_FROM: '#f59e0b',
    DEFINES: '#64748b',
    OVERRIDES: '#f43f5e',
};

const EDGE_STYLES = {
    CALLS: 'solid',
    INHERITS_FROM: 'solid',
    DEFINES: 'dashed',
    OVERRIDES: 'dashed',
};

// ---- Cytoscape Style ----
const cyStyle = [
    // Class nodes
    {
        selector: 'node[type="Class"]',
        style: {
            'background-color': '#8b5cf6',
            'border-color': '#a78bfa',
            'border-width': 2,
            'label': 'data(label)',
            'color': '#e2e8f0',
            'font-size': 11,
            'font-family': 'Inter, sans-serif',
            'font-weight': 500,
            'text-valign': 'bottom',
            'text-margin-y': 8,
            'width': 40,
            'height': 40,
            'shape': 'round-rectangle',
            'text-outline-width': 2,
            'text-outline-color': '#0a0e17',
            'text-max-width': 120,
            'text-wrap': 'ellipsis',
        },
    },
    // Function nodes
    {
        selector: 'node[type="Function"]',
        style: {
            'background-color': '#3b82f6',
            'border-color': '#60a5fa',
            'border-width': 2,
            'label': 'data(label)',
            'color': '#e2e8f0',
            'font-size': 10,
            'font-family': 'Inter, sans-serif',
            'text-valign': 'bottom',
            'text-margin-y': 8,
            'width': 28,
            'height': 28,
            'shape': 'ellipse',
            'text-outline-width': 2,
            'text-outline-color': '#0a0e17',
            'text-max-width': 100,
            'text-wrap': 'ellipsis',
        },
    },
    // Lambda nodes
    {
        selector: 'node[kind="lambda"]',
        style: {
            'background-color': '#14b8a6',
            'border-color': '#2dd4bf',
            'shape': 'diamond',
            'width': 24,
            'height': 24,
        },
    },
    // Constructor nodes
    {
        selector: 'node[kind="constructor"]',
        style: {
            'background-color': '#f59e0b',
            'border-color': '#fbbf24',
        },
    },
    // Selected / highlighted
    {
        selector: 'node:selected',
        style: {
            'border-color': '#ffffff',
            'border-width': 3,
            'background-opacity': 1,
        },
    },
    {
        selector: 'node.highlighted',
        style: {
            'border-color': '#10b981',
            'border-width': 3,
            'background-opacity': 1,
        },
    },
    {
        selector: 'node.dimmed',
        style: {
            'opacity': 0.2,
        },
    },
    // Edges
    {
        selector: 'edge',
        style: {
            'width': 1.5,
            'line-color': '#475569',
            'target-arrow-color': '#475569',
            'target-arrow-shape': 'triangle',
            'arrow-scale': 0.8,
            'curve-style': 'bezier',
            'opacity': 0.7,
        },
    },
    {
        selector: 'edge[rel="CALLS"]',
        style: {
            'line-color': '#3b82f6',
            'target-arrow-color': '#3b82f6',
            'width': 2,
        },
    },
    {
        selector: 'edge[rel="INHERITS_FROM"]',
        style: {
            'line-color': '#f59e0b',
            'target-arrow-color': '#f59e0b',
            'target-arrow-shape': 'triangle-tee',
            'width': 2.5,
            'line-style': 'solid',
        },
    },
    {
        selector: 'edge[rel="DEFINES"]',
        style: {
            'line-color': '#64748b',
            'target-arrow-color': '#64748b',
            'line-style': 'dashed',
            'width': 1,
            'opacity': 0.5,
        },
    },
    {
        selector: 'edge[rel="OVERRIDES"]',
        style: {
            'line-color': '#f43f5e',
            'target-arrow-color': '#f43f5e',
            'line-style': 'dashed',
            'width': 2,
        },
    },
    {
        selector: 'edge.dimmed',
        style: { 'opacity': 0.08 },
    },
    {
        selector: 'edge.highlighted',
        style: { 'opacity': 1, 'width': 3 },
    },
];


// ---- Init ----
document.addEventListener('DOMContentLoaded', () => {
    initCytoscape();
    loadStats();
    loadNamespaces();
    setupSearch();
    setupDepthSlider();
});

function initCytoscape() {
    state.cy = cytoscape({
        container: document.getElementById('cy'),
        style: cyStyle,
        layout: { name: 'preset' },
        minZoom: 0.1,
        maxZoom: 4,
        wheelSensitivity: 0.3,
    });

    // Node click
    state.cy.on('tap', 'node', (evt) => {
        const node = evt.target;
        state.selectedNode = node.data();
        showDetail(node.data());
        highlightNeighbors(node);
    });

    // Background click
    state.cy.on('tap', (evt) => {
        if (evt.target === state.cy) {
            clearHighlight();
            closeDetail();
        }
    });

    // Double-click to expand
    state.cy.on('dbltap', 'node', (evt) => {
        const data = evt.target.data();
        if (data.type === 'Class') {
            loadMethods(data.id);
        } else {
            loadNeighborhood(data.id, state.depth);
        }
    });
}


// ---- API Helpers ----
async function api(endpoint) {
    showLoading(true);
    try {
        const resp = await fetch(endpoint);
        if (!resp.ok) throw new Error(`API error: ${resp.status}`);
        return await resp.json();
    } catch (err) {
        console.error('API Error:', err);
        return null;
    } finally {
        showLoading(false);
    }
}


// ---- Data Loading ----
async function loadStats() {
    const data = await api('/api/stats');
    if (!data) return;

    let totalNodes = 0, totalEdges = 0, classes = 0, functions = 0;
    for (const n of data.nodes || []) {
        totalNodes += n.cnt;
        if (n.label === 'Class') classes = n.cnt;
        if (n.label === 'Function') functions = n.cnt;
    }
    for (const e of data.edges || []) totalEdges += e.cnt;

    document.getElementById('stat-nodes').textContent = formatNum(totalNodes);
    document.getElementById('stat-edges').textContent = formatNum(totalEdges);
    document.getElementById('stat-classes').textContent = formatNum(classes);
    document.getElementById('stat-functions').textContent = formatNum(functions);
}

async function loadNamespaces() {
    const data = await api('/api/namespaces');
    if (!data) return;

    const panel = document.getElementById('results-panel');
    if (data.length === 0) {
        panel.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted);font-size:13px;">No data in graph yet. Run the ingestion pipeline first.</div>';
        return;
    }

    panel.innerHTML = data.map(ns => `
        <div class="result-item" onclick="loadClassesForNamespace('${escHtml(ns.ns)}')">
            <div class="result-name">
                <span class="result-badge badge-class">NS</span>
                ${escHtml(ns.ns || 'global')}
            </div>
            <div class="result-meta">${ns.cnt} classes</div>
        </div>
    `).join('');
}

async function loadClassesForNamespace(ns) {
    const data = await api(`/api/classes?namespace=${encodeURIComponent(ns)}`);
    if (!data) return;

    setBreadcrumb(ns);

    // Show in results panel
    const panel = document.getElementById('results-panel');
    panel.innerHTML = `
        <div class="result-item" onclick="loadNamespaces()" style="color:var(--accent-blue);font-size:12px;">
            ← Back to namespaces
        </div>
    ` + data.map(c => `
        <div class="result-item" onclick="focusNode('${escHtml(c.usr)}'); loadNeighborhood('${escHtml(c.usr)}', ${state.depth})">
            <div class="result-name">
                <span class="result-badge badge-class">${c.kind === 'interface' ? 'IF' : 'CL'}</span>
                ${escHtml(c.name)}
            </div>
            <div class="result-fqn">${escHtml(c.fqn)}</div>
            <div class="result-meta">${escHtml(c.file)}:${c.line}</div>
        </div>
    `).join('');

    // Load class nodes into graph
    const elements = data.map(c => ({
        data: {
            id: c.usr, label: c.name, fqn: c.fqn,
            file: c.file, line: c.line, language: c.language,
            kind: c.kind, type: 'Class', namespace: c.namespace,
        }
    }));

    state.cy.elements().remove();
    state.cy.add(elements);
    runLayout();
    hideEmptyState();
}

async function loadMethods(classUsr) {
    const data = await api(`/api/methods?class_usr=${encodeURIComponent(classUsr)}`);
    if (!data || data.length === 0) return;

    const elements = [];
    for (const m of data) {
        // Add method node if not exists
        if (!state.cy.getElementById(m.usr).length) {
            elements.push({
                data: {
                    id: m.usr, label: m.name, fqn: m.fqn,
                    file: m.file, line: m.line, language: m.language,
                    kind: m.kind, type: 'Function',
                    parent_fqn: m.parent_fqn,
                }
            });
            elements.push({
                data: {
                    id: `${classUsr}-DEFINES-${m.usr}`,
                    source: classUsr, target: m.usr, rel: 'DEFINES',
                }
            });
        }
    }

    state.cy.add(elements);
    runLayout();
}

async function loadNeighborhood(usr, depth) {
    const data = await api(`/api/neighbors?usr=${encodeURIComponent(usr)}&hops=${depth}`);
    if (!data) return;
    addGraphData(data);
    focusNode(usr);
}

async function loadCallChain(usr, direction) {
    const d = state.depth;
    const data = await api(`/api/callchain?usr=${encodeURIComponent(usr)}&direction=${direction}&depth=${d}`);
    if (!data) return;

    state.cy.elements().remove();
    addGraphData(data);
    focusNode(usr);
    setBreadcrumb(`Call chain: ${direction}`);
}

async function loadInheritance(usr) {
    const data = await api(`/api/inheritance?usr=${encodeURIComponent(usr)}`);
    if (!data) return;

    state.cy.elements().remove();
    addGraphData(data);
    focusNode(usr);
    setBreadcrumb('Inheritance tree');
}


// ---- Graph Manipulation ----
function addGraphData(data) {
    const elements = [];
    const existingIds = new Set(state.cy.nodes().map(n => n.id()));

    for (const n of (data.nodes || [])) {
        if (!existingIds.has(n.data.id)) {
            elements.push(n);
            existingIds.add(n.data.id);
        }
    }

    for (const e of (data.edges || [])) {
        const eId = e.data.id;
        if (!state.cy.getElementById(eId).length) {
            // Only add if both endpoints exist
            if (existingIds.has(e.data.source) && existingIds.has(e.data.target)) {
                elements.push(e);
            }
        }
    }

    if (elements.length > 0) {
        state.cy.add(elements);
        applyEdgeFilters();
        runLayout();
        hideEmptyState();
    }
}

function applyEdgeFilters() {
    state.cy.edges().forEach(edge => {
        const rel = edge.data('rel');
        if (state.activeEdges.has(rel)) {
            edge.style('display', 'element');
        } else {
            edge.style('display', 'none');
        }
    });
}

function highlightNeighbors(node) {
    clearHighlight();
    const neighborhood = node.neighborhood().add(node);
    state.cy.elements().addClass('dimmed');
    neighborhood.removeClass('dimmed');
    neighborhood.edges().addClass('highlighted');
    node.addClass('highlighted');
}

function clearHighlight() {
    state.cy.elements().removeClass('dimmed highlighted');
}

function focusNode(usr) {
    const node = state.cy.getElementById(usr);
    if (node.length) {
        state.cy.animate({
            center: { eles: node },
            zoom: 1.5,
        }, { duration: 400 });
        node.select();
    }
}


// ---- Layout ----
function runLayout() {
    const opts = getLayoutOptions(state.currentLayout);
    state.cy.layout(opts).run();
}

function getLayoutOptions(name) {
    switch (name) {
        case 'dagre':
            return {
                name: 'dagre',
                rankDir: 'TB',
                nodeSep: 60,
                rankSep: 80,
                edgeSep: 20,
                animate: true,
                animationDuration: 400,
                fit: true,
                padding: 40,
            };
        case 'cose':
            return {
                name: 'cose',
                animate: true,
                animationDuration: 500,
                nodeRepulsion: 8000,
                idealEdgeLength: 100,
                edgeElasticity: 100,
                gravity: 0.25,
                fit: true,
                padding: 40,
            };
        case 'circle':
            return {
                name: 'circle',
                animate: true,
                animationDuration: 400,
                fit: true,
                padding: 40,
            };
        default:
            return { name: 'dagre', fit: true, padding: 40 };
    }
}

function changeLayout(name) {
    state.currentLayout = name;
    document.querySelectorAll('[id^="layout-"]').forEach(b => b.classList.remove('active'));
    document.getElementById(`layout-${name}`).classList.add('active');
    runLayout();
}


// ---- View Modes ----
function switchView(mode) {
    state.currentView = mode;
    document.querySelectorAll('#btn-namespace,#btn-callchain,#btn-inherit').forEach(b => b.classList.remove('active'));
    document.getElementById(`btn-${mode === 'callchain' ? 'callchain' : mode === 'inheritance' ? 'inherit' : 'namespace'}`).classList.add('active');

    if (mode === 'namespace') {
        resetGraph();
        loadNamespaces();
    } else if (mode === 'callchain' && state.selectedNode) {
        loadCallChain(state.selectedNode.id, 'both');
    } else if (mode === 'inheritance' && state.selectedNode) {
        loadInheritance(state.selectedNode.id);
    }
}


// ---- Edge Filters ----
function toggleEdge(rel) {
    const btn = document.getElementById(`filter-${rel}`);
    if (state.activeEdges.has(rel)) {
        state.activeEdges.delete(rel);
        btn.classList.remove('active');
    } else {
        state.activeEdges.add(rel);
        btn.classList.add('active');
    }
    applyEdgeFilters();
}


// ---- Search ----
function setupSearch() {
    const input = document.getElementById('search-input');
    input.addEventListener('input', () => {
        clearTimeout(state.searchTimeout);
        const q = input.value.trim();
        if (q.length < 2) {
            if (q.length === 0) loadNamespaces();
            return;
        }
        state.searchTimeout = setTimeout(() => searchNodes(q), 300);
    });
}

async function searchNodes(query) {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    if (!data) return;

    const panel = document.getElementById('results-panel');
    if (data.length === 0) {
        panel.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted);">No results found</div>';
        return;
    }

    panel.innerHTML = data.map(n => {
        const isFunc = n.label === 'Function';
        const badge = isFunc ? '<span class="result-badge badge-function">FN</span>' : '<span class="result-badge badge-class">CL</span>';
        return `
            <div class="result-item" onclick="selectSearchResult('${escHtml(n.usr)}', '${n.label}')">
                <div class="result-name">${badge} ${escHtml(n.name)}</div>
                <div class="result-fqn">${escHtml(n.fqn || '')}</div>
                <div class="result-meta">${escHtml(n.file || '')}${n.line ? ':' + n.line : ''}</div>
            </div>
        `;
    }).join('');
}

async function selectSearchResult(usr, label) {
    // Clear graph and load neighborhood
    state.cy.elements().remove();
    await loadNeighborhood(usr, state.depth);
}


// ---- Depth Slider ----
function setupDepthSlider() {
    const slider = document.getElementById('depth-slider');
    const value = document.getElementById('depth-value');
    slider.addEventListener('input', () => {
        state.depth = parseInt(slider.value);
        value.textContent = state.depth;
    });
}


// ---- Detail Panel ----
function showDetail(data) {
    const panel = document.getElementById('detail-panel');
    const content = document.getElementById('detail-content');

    const isFunc = data.type === 'Function';
    content.innerHTML = `
        <div class="detail-title">${escHtml(data.label)}</div>
        <div class="detail-fqn">${escHtml(data.fqn)}</div>

        <div class="detail-section">
            <div class="detail-section-title">Properties</div>
            <div class="detail-row"><span class="key">Type</span><span class="value">${data.type}</span></div>
            <div class="detail-row"><span class="key">Kind</span><span class="value">${data.kind}</span></div>
            <div class="detail-row"><span class="key">Language</span><span class="value">${data.language}</span></div>
            <div class="detail-row"><span class="key">File</span><span class="value">${escHtml(basename(data.file))}</span></div>
            <div class="detail-row"><span class="key">Line</span><span class="value">${data.line}</span></div>
            ${data.namespace ? `<div class="detail-row"><span class="key">Namespace</span><span class="value">${escHtml(data.namespace)}</span></div>` : ''}
            ${data.parent_fqn ? `<div class="detail-row"><span class="key">Parent</span><span class="value">${escHtml(data.parent_fqn)}</span></div>` : ''}
        </div>

        <div class="detail-section">
            <div class="detail-section-title">Actions</div>
            <div class="btn-row" style="flex-direction:column; gap:6px;">
                <button class="btn" onclick="loadNeighborhood('${escHtml(data.id)}', ${state.depth})" style="width:100%; text-align:left;">
                    Expand ${state.depth}-hop neighborhood
                </button>
                ${isFunc ? `
                    <button class="btn" onclick="loadCallChain('${escHtml(data.id)}', 'callers')" style="width:100%; text-align:left;">
                        ↑ Show callers
                    </button>
                    <button class="btn" onclick="loadCallChain('${escHtml(data.id)}', 'callees')" style="width:100%; text-align:left;">
                        ↓ Show callees
                    </button>
                    <button class="btn" onclick="loadCallChain('${escHtml(data.id)}', 'both')" style="width:100%; text-align:left;">
                        ↕ Full call chain
                    </button>
                ` : `
                    <button class="btn" onclick="loadMethods('${escHtml(data.id)}')" style="width:100%; text-align:left;">
                        Show methods
                    </button>
                    <button class="btn" onclick="loadInheritance('${escHtml(data.id)}')" style="width:100%; text-align:left;">
                        Show inheritance tree
                    </button>
                `}
            </div>
        </div>
    `;

    panel.classList.add('open');
}

function closeDetail() {
    document.getElementById('detail-panel').classList.remove('open');
}


// ---- Toolbar Actions ----
function fitGraph() {
    state.cy.fit(undefined, 40);
}

function resetGraph() {
    state.cy.elements().remove();
    clearHighlight();
    closeDetail();
    setBreadcrumb('');
    showEmptyState();
}

function exportPNG() {
    const png = state.cy.png({ scale: 2, bg: '#0a0e17', full: true });
    const link = document.createElement('a');
    link.href = png;
    link.download = 'omnigraph_export.png';
    link.click();
}


// ---- Utilities ----
function showLoading(show) {
    document.getElementById('loading').classList.toggle('hidden', !show);
}

function hideEmptyState() {
    document.getElementById('empty-state').style.display = 'none';
}

function showEmptyState() {
    document.getElementById('empty-state').style.display = 'flex';
}

function setBreadcrumb(text) {
    document.getElementById('breadcrumb').innerHTML = text ? `<span>${escHtml(text)}</span>` : '';
}

function formatNum(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
}

function basename(path) {
    if (!path) return '';
    return path.split('/').pop() || path;
}

function escHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
