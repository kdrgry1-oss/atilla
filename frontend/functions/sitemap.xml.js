/**
 * Cloudflare Pages Function — /sitemap.xml
 * Dinamik site haritasını (ürünler, kategoriler, sayfalar) backend'den proxy'ler → mağazanın
 * KENDİ alan adında erişilir (eskiden bu adres SPA'nın index.html'ini 200 ile döndürüyordu;
 * Search Console'a verilen site haritası "HTML" olarak okunamıyordu).
 * API adresi: env API_BASE, yoksa alan adından türetilir (magaza.com → api.magaza.com).
 */
export async function onRequest(context) {
  const { env, request } = context;
  const url = new URL(request.url);
  const host = String(url.hostname || "").replace(/^www\./, "");
  const apiBase = ((env && env.API_BASE) || (host ? `https://api.${host}/api` : ""))
    .replace(/\/api\/?$/, "");
  if (apiBase) {
    try {
      const r = await fetch(`${apiBase}/sitemap.xml`, {
        cf: { cacheTtl: 3600, cacheEverything: true },
        headers: { accept: "application/xml" },
      });
      if (r.ok) {
        const body = await r.text();
        if (body && (body.indexOf("<urlset") !== -1 || body.indexOf("<sitemapindex") !== -1)) {
          return new Response(body, {
            headers: {
              "content-type": "application/xml; charset=utf-8",
              "cache-control": "public, max-age=3600",
            },
          });
        }
      }
    } catch (_) {
      /* aşağıda 404 */
    }
  }
  // HTML (SPA) döndürmek yerine dürüst 404 — arama motoru yanlış içeriği site haritası sanmasın.
  return new Response("sitemap unavailable", { status: 404, headers: { "content-type": "text/plain" } });
}
