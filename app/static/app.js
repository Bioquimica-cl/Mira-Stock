console.log("[MiraStock-Total] Sistema cargado (v2.2 — solo stock tienda, categorías reales).");

let inputBuffer = '';
let bufferTimeout = null;
let resetTimeout = null;
let resetInterval = null;
let syncInterval = null;
let lastKnownRunning = false;
let currentPage = 1;
let catalogViewMode = localStorage.getItem('catalog-view') || 'grid';
let lastProducts = [];

// --- 1. Inicialización ---
async function init() {
    try {
        applyCatalogView();
        await startSyncPolling();
        await loadSyncSchedule();
        await loadCategories();
        initMobileSidebar();
        loadCatalog();

        const savedScale = parseFloat(localStorage.getItem('font-scale')) || 1.0;
        document.documentElement.style.fontSize = `${16 * savedScale}px`;
        document.documentElement.style.setProperty('--font-scale', savedScale);
    } catch (err) {
        console.error("Error en init:", err);
    }
}

// --- 2. Navegación ---
function toggleMobileMenu() {
    document.getElementById('secondary-nav')?.classList.toggle('mobile-open');
}

function toggleMobileFilters() {
    const sidebar = document.getElementById('catalog-sidebar');
    const chevron = document.getElementById('mobile-filter-chevron');
    if (!sidebar) return;
    const isHidden = sidebar.style.display === 'none';
    sidebar.style.display = isHidden ? 'block' : 'none';
    chevron?.classList.toggle('rotate-180', isHidden);
}

function initMobileSidebar() {
    const sidebar = document.getElementById('catalog-sidebar');
    if (!sidebar) return;
    if (window.innerWidth < 1024) sidebar.style.display = 'none';
}

window.addEventListener('resize', () => {
    const sidebar = document.getElementById('catalog-sidebar');
    if (!sidebar) return;
    if (window.innerWidth >= 1024) {
        sidebar.style.display = '';
        document.getElementById('mobile-filter-chevron')?.classList.remove('rotate-180');
    }
});

function showView(viewId) {
    const secondaryNav = document.getElementById('secondary-nav');
    if (secondaryNav && window.innerWidth < 768) {
        secondaryNav.classList.remove('mobile-open');
    }
    stopResetTimer();

    document.querySelectorAll('.view').forEach(v => {
        v.classList.add('hidden');
        v.style.opacity = '0';
    });
    document.querySelectorAll('.nav-btn').forEach(btn => btn.classList.remove('active'));

    if (viewId === 'loading') {
        const home = document.getElementById('home-view');
        home.classList.remove('hidden');
        home.style.opacity = '1';
        document.getElementById('status-text').innerHTML = `
            <div class="flex items-center justify-center gap-2 text-orange-600 font-bold">
                <i class="fas fa-circle-notch fa-spin"></i> Buscando...
            </div>`;
        document.getElementById('nav-home').classList.add('active');
        return;
    }

    const target = document.getElementById(viewId);
    if (!target) return;
    target.classList.remove('hidden');
    setTimeout(() => (target.style.opacity = '1'), 10);

    if (viewId === 'home-view' || viewId === 'product-view') {
        document.getElementById('nav-home').classList.add('active');
        if (viewId === 'home-view') {
            document.getElementById('status-text').innerHTML = '<p>Esperando escaneo...</p>';
        }
    } else if (viewId === 'catalog-view') {
        document.getElementById('nav-catalog').classList.add('active');
        loadCatalog();
    }
}

// --- 3. Escáner ---
document.addEventListener('keydown', (e) => {
    if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) return;
    if (!document.getElementById('product-modal').classList.contains('hidden')) return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;

    clearTimeout(bufferTimeout);

    if (e.key === 'Enter') {
        const trimmed = inputBuffer.trim();
        if (trimmed.length > 0) {
            lookupProduct(trimmed);
            inputBuffer = '';
        } else {
            resetApp();
        }
    } else if (e.key.length === 1) {
        inputBuffer += e.key;
        bufferTimeout = setTimeout(() => { inputBuffer = ''; }, 300);
    }
});

document.getElementById('manual-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const sku = document.getElementById('manual-sku').value.trim();
    if (sku) {
        lookupProduct(sku);
        document.getElementById('manual-sku').value = '';
        document.getElementById('manual-sku').blur();
    }
});

async function lookupProduct(sku) {
    showView('loading');
    try {
        const res = await fetch(`/api/product/${sku.toUpperCase()}`);
        const data = await res.json();
        if (res.ok && data.status === 'success') {
            displayProduct(data.product);
            startResetTimer();
        } else {
            showError('Producto no encontrado en el catálogo', sku);
            startResetTimer();
        }
    } catch (err) {
        console.error("Error en lookup:", err);
        showError(err && err.message ? `Error: ${err.message}` : 'Error de red o servidor', sku);
        startResetTimer();
    }
}

function displayProduct(p) {
    showView('product-view');

    document.getElementById('product-sku').textContent  = p.sku || '';
    document.getElementById('product-name').textContent = p.name || 'Sin nombre';
    document.getElementById('product-price').textContent = formatPrice(priceWithIva(p.price));

    const imgCol = document.getElementById('product-image-col');
    const imgEl  = document.getElementById('product-image');
    const urls   = p.image_urls || [];
    if (urls.length) {
        imgEl.src = urls[0];
        imgCol.classList.remove('hidden');
        imgCol.classList.add('flex');
    } else {
        imgCol.classList.add('hidden');
        imgCol.classList.remove('flex');
    }

    document.getElementById('product-stock-tienda').textContent = Math.round(p.stock_tienda || 0);
}

// --- 4. Catálogo ---
async function loadCatalog(search = '', page = 1) {
    const grid       = document.getElementById('catalog-grid');
    const loading    = document.getElementById('catalog-loading');
    const empty      = document.getElementById('catalog-empty');
    const pagination = document.getElementById('pagination-controls');

    if (search === null) search = document.getElementById('catalog-search').value;

    grid.innerHTML = '';
    document.getElementById('catalog-table-body').innerHTML = '';
    loading.classList.remove('hidden');
    empty.classList.add('hidden');
    pagination.innerHTML = '';

    try {
        const stock    = document.getElementById('catalog-stock-status').value;
        const category = document.getElementById('catalog-category').value;
        const url = `/api/products?search=${encodeURIComponent(search)}&stock_filter=${stock}&category=${encodeURIComponent(category)}&page=${page}`;
        const res  = await fetch(url);
        const data = await res.json();

        lastProducts = data.products || [];
        renderCatalogView(lastProducts);
        renderPagination(data.pagination || {});
        currentPage = data.pagination?.current_page || 1;
    } catch (err) {
        console.error("Error cargando catálogo:", err);
    } finally {
        loading.classList.add('hidden');
    }
}

function priceWithIva(price) {
    return Math.round((parseFloat(price) || 0) * 1.19);
}

// --- Selector de vista: tarjetas / tabla ---
function setCatalogView(mode) {
    catalogViewMode = mode;
    localStorage.setItem('catalog-view', mode);
    applyCatalogView();
    renderCatalogView(lastProducts);
}

function applyCatalogView() {
    const isTable = catalogViewMode === 'table';
    document.getElementById('catalog-grid')?.classList.toggle('hidden', isTable);
    document.getElementById('catalog-table')?.classList.toggle('hidden', !isTable);
    document.getElementById('view-btn-grid')?.classList.toggle('active', !isTable);
    document.getElementById('view-btn-table')?.classList.toggle('active', isTable);
}

function renderCatalogView(products) {
    const empty = document.getElementById('catalog-empty');
    if (!products.length) {
        empty.classList.remove('hidden');
        document.getElementById('catalog-grid').innerHTML = '';
        document.getElementById('catalog-table-body').innerHTML = '';
        return;
    }
    empty.classList.add('hidden');

    if (catalogViewMode === 'table') {
        renderCatalogTable(products);
    } else {
        renderCatalog(products);
    }
}

function renderCatalogTable(products) {
    const body = document.getElementById('catalog-table-body');
    body.innerHTML = '';

    products.forEach(p => {
        const precioTienda = priceWithIva(p.price);
        const tr = document.createElement('tr');
        tr.className = 'catalog-table-row';
        tr.innerHTML = `
            <td class="px-5 py-3 text-xs font-bold font-mono text-gray-600 whitespace-nowrap">${escapeHtml(p.sku)}</td>
            <td class="px-5 py-3 text-sm font-semibold text-gray-800">${escapeHtml(p.name)}</td>
            <td class="px-5 py-3 text-sm text-gray-500 catalog-desc-cell" title="${escapeHtml(p.description || '')}">${escapeHtml(p.description || '')}</td>
            <td class="px-5 py-3 text-sm font-black text-right whitespace-nowrap ${Math.round(p.stock_tienda||0)>0?'text-orange-500':'text-gray-300'}">${Math.round(p.stock_tienda||0)}</td>
            <td class="px-5 py-3 text-sm font-bold text-slate-800 text-right whitespace-nowrap">${formatPrice(precioTienda)}</td>
        `;
        tr.onclick = () => openProductModal(p);
        body.appendChild(tr);
    });
}

function renderCatalog(products) {
    const grid = document.getElementById('catalog-grid');
    grid.innerHTML = '';

    products.forEach(p => {
        const precioTienda = priceWithIva(p.price);
        const imgSrc = (p.image_urls && p.image_urls[0]) || 'img/logo_bioquimica.png';

        const card = document.createElement('div');
        card.className = 'product-card group bg-white rounded-[2rem] border border-gray-100 overflow-hidden shadow-sm hover:shadow-xl transition-all duration-300 flex flex-col';
        card.dataset.sku = p.sku;
        card.innerHTML = `
            <div class="aspect-square w-full bg-gray-50 flex items-center justify-center p-6 overflow-hidden relative">
                <img src="${imgSrc}" alt="${escapeHtml(p.name)}"
                    class="w-full h-full object-contain transition-transform duration-500 group-hover:scale-110"
                    onerror="this.src='img/logo_bioquimica.png'">
            </div>
            <div class="p-5 flex-1 flex flex-col gap-3">
                <div>
                    <h3 class="text-sm font-semibold text-gray-800 leading-snug line-clamp-2 mb-1">${escapeHtml(p.name)}</h3>
                    <span class="text-xs font-bold font-mono text-gray-600">${p.sku}</span>
                </div>

                <div class="mt-auto space-y-2">
                    <div class="flex items-center gap-3">
                        <span class="text-xs font-bold text-gray-500 uppercase">Stock tienda</span>
                        <span class="text-sm font-black ${Math.round(p.stock_tienda||0)>0?'text-orange-500':'text-gray-300'}">${Math.round(p.stock_tienda||0)}</span>
                    </div>

                    <div class="pt-2 border-t border-gray-100">
                        <div class="flex items-center justify-between">
                            <span class="text-xs font-bold text-gray-500 uppercase">Precio tienda</span>
                            <span class="text-base font-bold text-slate-800">${formatPrice(precioTienda)}</span>
                        </div>
                    </div>

                    <button class="ver-mas-btn w-full py-2 rounded-xl bg-orange-50 hover:bg-orange-100 text-orange-600 font-bold text-xs transition-all active:scale-95 flex items-center justify-center gap-1.5">
                        <i class="fas fa-expand-alt text-[10px]"></i> Ver más
                    </button>
                </div>
            </div>
        `;
        card.querySelector('.ver-mas-btn').onclick = () => openProductModal(p);
        grid.appendChild(card);
    });
}

function renderPagination(info) {
    const container = document.getElementById('pagination-controls');
    container.innerHTML = '';
    if (!info.total_pages || info.total_pages <= 1) return;

    const prev = document.createElement('button');
    prev.className = `px-4 py-2 rounded-xl font-bold transition-all ${info.current_page > 1 ? 'bg-white hover:bg-gray-50 border text-gray-700' : 'bg-gray-50 text-gray-300 cursor-not-allowed border'}`;
    prev.innerHTML = '<i class="fas fa-chevron-left"></i>';
    prev.onclick = () => info.current_page > 1 && loadCatalog(null, info.current_page - 1);
    container.appendChild(prev);

    const pageInfo = document.createElement('span');
    pageInfo.className = "px-6 py-2 font-bold text-gray-600 bg-gray-100 rounded-xl";
    pageInfo.textContent = `Página ${info.current_page} de ${info.total_pages} (${info.total_items} ítems)`;
    container.appendChild(pageInfo);

    const next = document.createElement('button');
    next.className = `px-4 py-2 rounded-xl font-bold transition-all ${info.current_page < info.total_pages ? 'bg-white hover:bg-gray-50 border text-gray-700' : 'bg-gray-50 text-gray-300 cursor-not-allowed border'}`;
    next.innerHTML = '<i class="fas fa-chevron-right"></i>';
    next.onclick = () => info.current_page < info.total_pages && loadCatalog(null, info.current_page + 1);
    container.appendChild(next);
}

function clearFilters() {
    document.getElementById('catalog-search').value = '';
    document.getElementById('catalog-stock-status').value = 'instock';
    document.getElementById('catalog-category').value = '';

    loadCatalog('', 1);
}

// --- 5. Filtros catálogo ---
let searchTimeout;
document.getElementById('catalog-search').addEventListener('input', (e) => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => loadCatalog(e.target.value, 1), 400);
});
document.getElementById('catalog-stock-status').addEventListener('change', () =>
    loadCatalog(document.getElementById('catalog-search').value, 1));
document.getElementById('catalog-category').addEventListener('change', () =>
    loadCatalog(document.getElementById('catalog-search').value, 1));

async function loadCategories() {
    try {
        const res  = await fetch('/api/categories');
        if (!res.ok) return;
        const cats = await res.json();
        const sel  = document.getElementById('catalog-category');
        if (!sel) return;
        cats.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.slug;
            opt.textContent = c.name;
            sel.appendChild(opt);
        });
    } catch (err) {
        console.error("Error cargando categorías:", err);
    }
}

// --- 6. Modal de detalle ---
function openProductModal(p) {
    document.getElementById('modal-sku').textContent   = p.sku;
    document.getElementById('modal-title').textContent = p.name;
    document.getElementById('modal-price').textContent = formatPrice(priceWithIva(p.price));

    // Descripción
    const descWrap = document.getElementById('modal-description-wrap');
    const descEl   = document.getElementById('modal-description');
    if (p.description) {
        if (descEl) descEl.innerHTML = p.description;
        descWrap?.classList.remove('hidden');
    } else {
        descWrap?.classList.add('hidden');
    }

    // Ubicación en tienda (solo lectura)
    const locWrap = document.getElementById('modal-location-wrap');
    const locText = document.getElementById('modal-location-text');
    if (locWrap && locText) {
        if (p.location) {
            locText.textContent = p.location;
            locText.className = 'text-sm font-bold text-slate-700 bg-amber-50 border border-amber-100 rounded-xl px-4 py-3 min-h-[44px]';
            locWrap.classList.remove('hidden');
        } else {
            locWrap.classList.add('hidden');
        }
    }

    // Stock tienda
    const wlDiv  = document.getElementById('modal-warehouse-list');
    const tienda = Math.round(p.stock_tienda || 0);
    wlDiv.innerHTML = `
        <div class="bg-orange-50 rounded-xl p-3 text-center border border-orange-100">
            <span class="text-[9px] font-bold text-orange-400 uppercase block mb-1">Tienda</span>
            <span class="text-2xl font-black ${tienda > 0 ? 'text-orange-600' : 'text-gray-300'}">${tienda}</span>
        </div>`;

    // Galería modal
    initModalGallery(p);

    document.getElementById('product-modal').classList.remove('hidden');
    document.body.style.overflow = 'hidden';
}

function closeProductModal() {
    document.getElementById('product-modal').classList.add('hidden');
    document.body.style.overflow = 'auto';
}

// --- 7. Sincronización ---
async function startSyncPolling() {
    if (syncInterval) clearInterval(syncInterval);
    syncInterval = setInterval(async () => {
        try {
            const res    = await fetch('/api/sync-status');
            const status = await res.json();
            const text    = document.getElementById('sync-text');
            const spinner = document.getElementById('sync-spinner');

            if (status.is_running) {
                text.textContent = `Sincronizando SAP... (${status.progress}%)`;
                spinner.classList.remove('hidden');
                lastKnownRunning = true;
            } else {
                spinner.classList.add('hidden');
                text.textContent = status.last_sync
                    ? `Actualizado: ${status.last_sync}`
                    : status.message || 'Sin sincronizar';

                if (lastKnownRunning && status.progress === 100) {
                    loadCatalog(document.getElementById('catalog-search').value);
                    loadSyncSchedule();
                    lastKnownRunning = false;
                }
            }
        } catch (err) {
            console.error("Error en polling de sync:", err);
        }
    }, 2000);
}

async function loadSyncSchedule() {
    try {
        const res  = await fetch('/api/sync-schedule');
        if (!res.ok) return;
        const data = await res.json();
        const el   = document.getElementById('next-sync-text');
        if (!el) return;
        if (data.next_sync) {
            const time = data.next_sync.split(' ')[1]?.slice(0, 5) || data.next_sync;
            el.textContent = `Próx. sync: ${time} (c/${data.interval_minutes}m)`;
        } else {
            el.textContent = `Auto-sync: cada ${data.interval_minutes}m`;
        }
    } catch (err) {
        console.error("Error cargando schedule:", err);
    }
}

// --- 8. Utilidades ---
function formatPrice(price) {
    const n = parseInt(price) || 0;
    if (n === 0) return '$0';
    return `$${n.toLocaleString('es-CL')}`;
}

function escapeHtml(str) {
    return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showError(message, sku = '') {
    document.getElementById('error-message').textContent = message;
    document.getElementById('error-sku').textContent = sku ? `SKU buscado: ${sku}` : '';
    showView('error-view');
}

function startResetTimer() {
    stopResetTimer();
    let timeLeft = 100;
    const p1 = document.getElementById('reset-progress');
    const p2 = document.getElementById('reset-progress-error');

    document.getElementById('restart-btn-product').classList.add('hidden');
    document.getElementById('restart-btn-error').classList.add('hidden');

    resetInterval = setInterval(() => {
        timeLeft -= 2;
        if (p1) p1.style.width = `${timeLeft}%`;
        if (p2) p2.style.width = `${timeLeft}%`;
        if (timeLeft <= 0) clearInterval(resetInterval);
    }, 100);

    resetTimeout = setTimeout(() => {
        document.getElementById('restart-btn-product').classList.remove('hidden');
        document.getElementById('restart-btn-error').classList.remove('hidden');
    }, 5000);
}

function stopResetTimer() {
    clearTimeout(resetTimeout);
    clearInterval(resetInterval);
}

function resetApp() {
    showView('home-view');
    document.getElementById('manual-sku').focus();
}

function changeFontSize(delta) {
    const root = document.documentElement;
    let scale = Math.round(((parseFloat(localStorage.getItem('font-scale')) || 1.0) + delta) * 10) / 10;
    scale = Math.min(1.8, Math.max(0.7, scale));
    root.style.fontSize = `${16 * scale}px`;
    root.style.setProperty('--font-scale', scale);
    localStorage.setItem('font-scale', scale);
}

// --- 9. Galería modal (solo visualización) ---
let galleryUrls = [];
let galleryIdx  = 0;

function initModalGallery(p) {
    galleryUrls = p.image_urls || [];
    galleryIdx  = 0;

    const col  = document.getElementById('modal-image-col');
    const imgEl = document.getElementById('modal-image');
    const dots = document.getElementById('modal-img-dots');
    const prev = document.getElementById('modal-img-prev');
    const next = document.getElementById('modal-img-next');

    if (!galleryUrls.length) {
        col.classList.add('hidden');
        col.classList.remove('flex');
        if (imgEl) imgEl.src = '';
        if (dots)  dots.innerHTML = '';
        if (prev)  prev.classList.add('hidden');
        if (next)  next.classList.add('hidden');
    } else {
        col.classList.remove('hidden');
        col.classList.add('flex');
        renderGalleryFrame();
    }
}

function renderGalleryFrame() {
    const imgEl = document.getElementById('modal-image');
    const dots  = document.getElementById('modal-img-dots');
    const prev  = document.getElementById('modal-img-prev');
    const next  = document.getElementById('modal-img-next');

    if (imgEl) imgEl.src = galleryUrls[galleryIdx] || '';

    if (dots) {
        const showDots = galleryUrls.length > 1;
        dots.innerHTML = showDots
            ? galleryUrls.map((_, i) => `
                <button onclick="modalGalleryNav(${i - galleryIdx})"
                    class="w-2 h-2 rounded-full transition-all ${i === galleryIdx ? 'bg-orange-500 scale-125' : 'bg-gray-300 hover:bg-gray-400'}">
                </button>`).join('')
            : '';
    }

    const multi = galleryUrls.length > 1;
    if (prev) prev.classList.toggle('hidden', !multi || galleryIdx === 0);
    if (next) next.classList.toggle('hidden', !multi || galleryIdx === galleryUrls.length - 1);
}

function modalGalleryNav(delta) {
    galleryIdx = Math.max(0, Math.min(galleryUrls.length - 1, galleryIdx + delta));
    renderGalleryFrame();
}

// Iniciar
init();
