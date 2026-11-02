/**
 * =============================================================================
 * Cloudflare Pages Edge SEO Middleware — functions/_middleware.js
 * =============================================================================
 * SORUN (SEO denetimi P0): Site tamamen client-side render (CRA). Her sayfanın İLK
 * HTML'i public/index.html → HEPSİNDE aynı <title>, aynı description ve
 * ana sayfa canonical değeri vardı. Google tüm ürün/
 * kategori URL'lerini ANA SAYFAYA canonical'ledi → ürün/kategori sayfaları
 * indekslenmiyordu (uzun-kuyruk trafiğinin tamamı kayıp).
 *
 * ÇÖZÜM: Her HTML isteğinde backend'den /api/seo/page-meta?path=... çekilir ve
 * dönen title/description/canonical/OG + (ürün) JSON-LD ilk HTML'in <head>'ine
 * HTMLRewriter ile enjekte edilir. Böylece crawler ilk HTML'de sayfaya-özel
 * doğru meta'yı görür. Bilinmeyen yollar noindex olur (soft-404 giderilir).
 *
 * GÜVENLİK/DAYANIKLILIK: Sadece text/html yanıtlarında çalışır (asset'ler
 * dokunulmaz). Meta çekimi zaman-aşımlı (2.5sn) ve HER hatada sayfayı OLDUĞU GİBİ
 * döndürür (fail-open) → mağazayı asla kırmaz. Yanıt edge'de kısa süre cache'lenir.
 * Client tarafı (ProductDetail.jsx) data-seo="edge" markerını görürse JSON-LD'yi
 * tekrar eklemez (duplicate önlenir).
 * =============================================================================
 */

const NONHTML_EXT = /\.(js|css|map|png|jpe?g|webp|avif|gif|svg|ico|woff2?|ttf|eot|mp4|webm|json|xml|txt|pdf|wasm)$/i;

// JSON-LD güvenli gömme: </script> ve < kaçırma
function jsonForScript(obj) {
  try {
    return JSON.stringify(obj).replace(/</g, "\\u003c");
  } catch (_) {
    return "";
  }
}

// magaza.com / www.magaza.com → https://api.magaza.com/api (önizleme *.pages.dev → yok)
function deriveApiBase(url) {
  const host = String(url.hostname || "").replace(/^www\./, "");
  if (!host || host.endsWith(".pages.dev") || host === "localhost") return "";
  return `https://api.${host}/api`;
}

// Ana sayfadaki İLK yayındaki hero bloğunun ilk görseli (mobil/masaüstü ayrı) — vitrinin (lib/img.js
// optimizeImg) isteyeceği URL ile aynı: R2 cdn-cgi dönüşümü soyulur; dönüşümlü/backend görselleri atlanır.
function preloadSrc(u) {
  u = String(u || "").trim();
  if (!/^https:\/\//.test(u) || u.includes("ticimax") || u.includes("/api/")) return "";
  const m = u.match(/^(https:\/\/[^/]+)\/cdn-cgi\/image\/[^/]+\/(.+)$/);
  if (m) u = `${m[1]}/${m[2]}`;
  return /\.(webp|jpe?g|png|avif)(\?.*)?$/i.test(u) ? u : "";
}

async function homeHeroPreloads(apiBase) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 1500);
  let blocks = [];
  try {
    const r = await fetch(`${apiBase}/page-blocks?page=home`, {
      signal: ctrl.signal, cf: { cacheTtl: 300, cacheEverything: true }, headers: { accept: "application/json" },
    });
    if (r.ok) blocks = await r.json();
  } finally { clearTimeout(t); }
  if (!Array.isArray(blocks)) return null;
  const heroes = blocks.filter((b) => b && b.type === "hero_slider" && b.is_active !== false
    && Array.isArray(b.images) && b.images.length)
    .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0));
  let mobile = null, desktop = null;
  for (const b of heroes) {
    const src = preloadSrc(b.images[0]);
    if (mobile === null && b.show_mobile !== false) mobile = src;
    if (desktop === null && b.show_desktop !== false) desktop = src;
    if (mobile !== null && desktop !== null) break;
  }
  if (mobile && mobile === desktop) return [{ href: mobile }];
  const out = [];
  if (mobile) out.push({ href: mobile, media: "(max-width: 767px)" });
  if (desktop) out.push({ href: desktop, media: "(min-width: 768px)" });
  return out;
}

export async function onRequest(context) {
  const { request, next, env } = context;

  // 1) Yalnız GET; asset uzantılı yollar → dokunma
  let url;
  try {
    url = new URL(request.url);
  } catch (_) {
    return next();
  }
  const path = url.pathname || "/";
  // /.well-known/* altında yayınlanmış dosya yoksa SPA index.html'i 200 ile dönmesin: ajanlar ve
  // Lighthouse (ai-catalog.json "Malformed JSON") HTML'i manifest sanıyordu. Dosya varsa olduğu gibi.
  if (path.startsWith("/.well-known/")) {
    const r = await next();
    if ((r.headers.get("content-type") || "").includes("text/html")) {
      return new Response("Not found", { status: 404, headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    return r;
  }
  if (request.method !== "GET" || NONHTML_EXT.test(path) || path.startsWith("/api/")) {
    return next();
  }

  // 2) SPA yanıtını al (index.html)
  const response = await next();
  try {
    const ct = response.headers.get("content-type") || "";
    if (!ct.includes("text/html")) return response;

    // 3) Backend'den sayfa meta'sını çek (zaman-aşımlı, edge-cache'li, fail-open)
    // API_BASE deployment binding is the bootstrap address. Brand/domain data
    // itself comes from canonical tenant_config via this endpoint.
    // API_BASE tanımlı değilse (2026-09-08'den beri Pages ortamında yoktu → ara katman HİÇ
    // çalışmıyor, tüm kategori/ürün sayfaları ilk HTML'de ana sayfaya canonical'lanıyordu)
    // mağazanın kendi alan adından türetilir: magaza.com → https://api.magaza.com/api
    const apiBase = (env && env.API_BASE) || deriveApiBase(url);
    if (!apiBase) return response;
    const metaUrl = `${apiBase}/seo/page-meta?path=${encodeURIComponent(path)}`;
    let meta = null;
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 2500);
      const r = await fetch(metaUrl, {
        signal: ctrl.signal,
        cf: { cacheTtl: 300, cacheEverything: true },
        headers: { accept: "application/json" },
      });
      clearTimeout(t);
      if (r.ok) meta = await r.json();
    } catch (_) {
      meta = null;
    }
    if (!meta || meta.found !== true) return response;

    // 4) HTMLRewriter ile <head>'i güncelle
    const rw = new HTMLRewriter();

    // Boş değer mevcut (index.html) etiketini SİLMEZ — yalnız dolu değer yazılır.
    if (meta.title) {
      rw.on("title", {
        element(el) { el.setInnerContent(meta.title); },
      });
    }
    if (meta.description) {
      rw.on('meta[name="description"]', {
        element(el) { el.setAttribute("content", meta.description); },
      });
    }
    if (meta.canonical) {
      rw.on('link[rel="canonical"]', {
        element(el) { el.setAttribute("href", meta.canonical); },
      });
    }
    // Open Graph (index.html'de mevcut etiketleri yerinde güncelle)
    const ogMap = {
      "og:title": meta.og_title || meta.title,
      "og:description": meta.og_description || meta.description,
      "og:url": meta.og_url || meta.canonical,
      "og:image": meta.og_image,
      "og:type": meta.og_type,
      "og:site_name": meta.og_site_name,
      "og:locale": meta.og_locale,
    };
    for (const [prop, val] of Object.entries(ogMap)) {
      if (!val) continue;
      rw.on(`meta[property="${prop}"]`, {
        element(el) { el.setAttribute("content", val); },
      });
    }

    const twitterMap = {
      "twitter:title": meta.title,
      "twitter:description": meta.description,
      "twitter:image": meta.og_image,
    };
    for (const [name, val] of Object.entries(twitterMap)) {
      if (!val) continue;
      rw.on(`meta[name="${name}"]`, {
        element(el) { el.setAttribute("content", val); },
      });
    }

    // <head> sonuna: robots (varsa) + JSON-LD (ürün) + edge marker
    const headAppend = [];
    if (meta.robots) {
      rw.on('meta[name="robots"]', {
        element(el) {
          el.setAttribute("content", meta.robots);
          el.setAttribute("data-seo", "edge");
        },
      });
    }
    if (Array.isArray(meta.jsonld)) {
      for (const item of meta.jsonld) {
        const j = jsonForScript(item);
        if (j) headAppend.push(`<script type="application/ld+json" data-seo="edge">${j}</script>`);
      }
    }
    // HIZ (LCP): ilk hero görseli HTML ile birlikte indirilmeye başlasın (yalnız https, en çok 2).
    // Backend page-meta preload_images vermezse ana sayfada yayındaki bloklardan EDGE'de hesaplanır.
    let preloads = Array.isArray(meta.preload_images) ? meta.preload_images : null;
    if (!preloads && path === "/") {
      try { preloads = await homeHeroPreloads(apiBase); } catch (_) { preloads = null; }
    }
    if (Array.isArray(preloads)) {
      for (const pi of preloads.slice(0, 2)) {
        const href = String((pi && pi.href) || "");
        if (!/^https:\/\/[^\s"'<>]+$/.test(href)) continue;
        const media = String((pi && pi.media) || "");
        const mediaAttr = /^\((max|min)-width: \d{3,4}px\)$/.test(media) ? ` media="${media}"` : "";
        headAppend.push(`<link rel="preload" as="image" href="${href}" fetchpriority="high"${mediaAttr} data-seo="edge">`);
      }
    }
    // İstemciye "edge meta enjekte edildi" işareti (twitter card da ekle)
    headAppend.push('<meta name="twitter:card" content="summary_large_image" data-seo="edge">');
    if (headAppend.length) {
      rw.on("head", {
        element(el) { el.append("\n" + headAppend.join("\n") + "\n", { html: true }); },
      });
    }

    return rw.transform(response);
  } catch (_) {
    // Herhangi bir hata → sayfayı olduğu gibi döndür (mağazayı kırma)
    return response;
  }
}
