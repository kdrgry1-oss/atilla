// craco.config.js
const path = require("path");
require("dotenv").config();

// Check if we're in development/preview mode (not production build)
// Craco sets NODE_ENV=development for start, NODE_ENV=production for build
const isDevServer = process.env.NODE_ENV !== "production";

// ── Build kimliği (T19) ─────────────────────────────────────────────────────────
// Vitrin bütünlük izleyicisi (backend security/storefront_integrity.py) yeni bir paketin
// MEŞRU deploy mu yoksa izinsiz yükleme mi olduğunu ayırt edebilsin diye derleme, kaynak
// git commit'ini gömer: index.html <meta name="build-sha"> + build/version.json.
// Kaynak sırası: Cloudflare Pages (CF_PAGES_COMMIT_SHA / CF_PAGES_BRANCH) → GitHub Actions
// (GITHUB_SHA / GITHUB_REF_NAME) → elle verilmiş REACT_APP_BUILD_SHA → `git rev-parse` →
// "unknown". Yeni npm bağımlılığı YOK (yalnız Node yerleşik child_process).
function _git(args) {
  try {
    return require("child_process")
      .execFileSync("git", args, { stdio: ["ignore", "pipe", "ignore"], timeout: 3000 })
      .toString()
      .trim();
  } catch (_) {
    return "";
  }
}

function resolveBuildInfo(env) {
  const e = env || process.env;
  const shaRaw = e.CF_PAGES_COMMIT_SHA || e.GITHUB_SHA || e.REACT_APP_BUILD_SHA || _git(["rev-parse", "HEAD"]);
  const branchRaw = e.CF_PAGES_BRANCH || e.GITHUB_REF_NAME || _git(["rev-parse", "--abbrev-ref", "HEAD"]);
  // Biçim doğrulaması: değer index.html'e kaçışsız yazılır (InterpolateHtmlPlugin) → yalnız
  // onaltılık commit ve güvenli dal adı karakterleri kabul edilir.
  const sha = /^[0-9a-f]{7,40}$/i.test(String(shaRaw || "").trim()) ? String(shaRaw).trim().toLowerCase() : "unknown";
  const br = String(branchRaw || "").trim();
  const branch = /^[A-Za-z0-9._\/-]{1,100}$/.test(br) && br !== "HEAD" ? br : "unknown";
  return { sha, branch, built_at: new Date().toISOString() };
}

const BUILD_INFO = resolveBuildInfo(process.env);
// CRA, REACT_APP_* değişkenlerini webpack yapılandırması kurulurken okur; craco bu dosyayı
// ondan ÖNCE yüklediği için burada atamak hem %REACT_APP_BUILD_SHA% (index.html) hem de
// process.env.REACT_APP_BUILD_SHA (JS) için yeterlidir.
process.env.REACT_APP_BUILD_SHA = BUILD_INFO.sha;
process.env.REACT_APP_BUILD_BRANCH = BUILD_INFO.branch;

// version.json'u derleme çıktısına yazan küçük satır içi webpack 5 eklentisi.
class BuildInfoPlugin {
  constructor(info) {
    this.info = info;
  }
  apply(compiler) {
    const wp = compiler.webpack;
    compiler.hooks.thisCompilation.tap("BuildInfoPlugin", (compilation) => {
      compilation.hooks.processAssets.tap(
        { name: "BuildInfoPlugin", stage: wp.Compilation.PROCESS_ASSETS_STAGE_ADDITIONAL },
        () => {
          compilation.emitAsset("version.json", new wp.sources.RawSource(JSON.stringify(this.info) + "\n"));
        }
      );
    });
  }
}

// Environment variable overrides
const config = {
  enableHealthCheck: process.env.ENABLE_HEALTH_CHECK === "true",
};

// Conditionally load health check modules only if enabled
let WebpackHealthPlugin;
let setupHealthEndpoints;
let healthPluginInstance;

if (config.enableHealthCheck) {
  WebpackHealthPlugin = require("./plugins/health-check/webpack-health-plugin");
  setupHealthEndpoints = require("./plugins/health-check/health-endpoints");
  healthPluginInstance = new WebpackHealthPlugin();
}

let webpackConfig = {
  eslint: {
    // Production build'de (Cloudflare/CI) ESLint'i kapat — exhaustive-deps gibi
    // zararsız uyarılar CI=true yüzünden "hata" sayılıp build'i durdurmasın.
    // Dev/preview modunda ESLint açık kalır.
    enable: isDevServer,
    configure: {
      extends: ["plugin:react-hooks/recommended"],
      rules: {
        "react-hooks/rules-of-hooks": "error",
        "react-hooks/exhaustive-deps": "warn",
      },
    },
  },
  webpack: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
    configure: (webpackConfig) => {

      // Add ignored patterns to reduce watched directories
        webpackConfig.watchOptions = {
          ...webpackConfig.watchOptions,
          ignored: [
            '**/node_modules/**',
            '**/.git/**',
            '**/build/**',
            '**/dist/**',
            '**/coverage/**',
            '**/public/**',
        ],
      };

      // Add health check plugin to webpack if enabled
      if (config.enableHealthCheck && healthPluginInstance) {
        webpackConfig.plugins.push(healthPluginInstance);
      }
      // T19: build kimliği → build/version.json (yalnız üretim derlemesi)
      if (webpackConfig.mode === "production") {
        webpackConfig.plugins.push(new BuildInfoPlugin(BUILD_INFO));
      }
      return webpackConfig;
    },
  },
};

webpackConfig.devServer = (devServerConfig) => {
  // Add health check endpoints if enabled
  if (config.enableHealthCheck && setupHealthEndpoints && healthPluginInstance) {
    const originalSetupMiddlewares = devServerConfig.setupMiddlewares;

    devServerConfig.setupMiddlewares = (middlewares, devServer) => {
      // Call original setup if exists
      if (originalSetupMiddlewares) {
        middlewares = originalSetupMiddlewares(middlewares, devServer);
      }

      // Setup health endpoints
      setupHealthEndpoints(devServer, healthPluginInstance);

      return middlewares;
    };
  }

  return devServerConfig;
};

// NOT: emergent.sh 'visual-edits' dev aracı kaldırıldı — tarball'ı (assets.emergent.sh)
// erişilemez olup npm install'ı kilitliyor, Cloudflare build'lerini bozuyordu. Prod/dev'de
// gerekli değil.

module.exports = webpackConfig;
// Test/denetim için; sayılamaz (non-enumerable) → craco'nun yapılandırma anahtarlarına karışmaz.
Object.defineProperty(module.exports, "__buildInfo", {
  value: { resolveBuildInfo, BuildInfoPlugin, BUILD_INFO },
  enumerable: false,
});
