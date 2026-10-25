// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The HTTP server for Anki's web views (aqt.mediasrv). Bundled files, add-on
//! web exports, collection media and legacy page HTML are served here without
//! the GIL. Other requests (mainly the /_anki/ API) are passed to Python.

use std::collections::HashMap;
use std::net::TcpListener as StdTcpListener;
use std::path::Component;
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::RwLock;
use std::thread::JoinHandle;

use axum::body::Body;
use axum::body::Bytes;
use axum::extract::Request;
use axum::extract::State;
use axum::http::header;
use axum::http::HeaderMap;
use axum::http::HeaderValue;
use axum::http::Method;
use axum::http::StatusCode;
use axum::response::Response;
use axum::Router;
use percent_encoding::percent_decode_str;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use regex::Regex;
use tokio::sync::oneshot;
use tower_http::services::ServeFile;

const LOCALHOST_HOSTS: [&str; 3] = ["127.0.0.1", "localhost", "[::1]"];

const SVELTEKIT_ROUTES: [&str; 11] = [
    "graphs",
    "congrats",
    "card-info",
    "change-notetype",
    "deck-options",
    "import-anki-package",
    "import-csv",
    "import-page",
    "image-occlusion",
    "preferences",
    "editor",
];

/// Routes that show user content.
const UNTRUSTED_SVELTEKIT_ROUTES: [&str; 2] = ["editor", "image-occlusion"];

/// Must match PageContext.EDITOR in aqt.mediasrv.
const PAGE_CONTEXT_EDITOR: u8 = 2;

/// Content-Security-Policy values, built by aqt.mediasrv.
#[derive(Default, Clone)]
struct Csp {
    trusted_page: String,
    untrusted_media: String,
    legacy_editor: String,
    /// Contains "{hash}" where the render script hash goes, if there is one.
    untrusted_sveltekit: String,
}

struct Page {
    html: String,
    context: u8,
}

enum WebExport {
    Regex(Regex),
    /// The pattern is valid in Python but not in Rust, so Python matches it.
    Python,
}

struct Shared {
    web_root: PathBuf,
    addons_root: PathBuf,
    allow_any_host: bool,
    csp: RwLock<Csp>,
    media_dir: RwLock<Option<PathBuf>>,
    web_exports: RwLock<HashMap<String, WebExport>>,
    pages: RwLock<HashMap<u64, Page>>,
    /// aqt.mediasrv._PythonHandler
    python: Py<PyAny>,
    filter_media_paths: AtomicBool,
    python_routes: AtomicBool,
}

#[pyclass(module = "_rsbridge")]
pub(crate) struct MediaServer {
    shared: Arc<Shared>,
    port: u16,
    host: String,
    shutdown: Option<oneshot::Sender<()>>,
    thread: Option<JoinHandle<()>>,
}

#[pymethods]
impl MediaServer {
    #[new]
    #[pyo3(signature = (host, port, web_root, addons_root, python, allow_any_host=false))]
    fn new(
        host: String,
        port: u16,
        web_root: PathBuf,
        addons_root: PathBuf,
        python: Py<PyAny>,
        allow_any_host: bool,
    ) -> PyResult<Self> {
        // binding here means the port is known as soon as this returns
        let listener = StdTcpListener::bind((host.as_str(), port))
            .map_err(|e| PyException::new_err(format!("unable to listen: {e}")))?;
        listener
            .set_nonblocking(true)
            .map_err(|e| PyException::new_err(e.to_string()))?;
        let port = listener
            .local_addr()
            .map_err(|e| PyException::new_err(e.to_string()))?
            .port();
        let shared = Arc::new(Shared {
            web_root,
            addons_root,
            allow_any_host,
            csp: Default::default(),
            media_dir: Default::default(),
            web_exports: Default::default(),
            pages: Default::default(),
            python,
            filter_media_paths: AtomicBool::new(false),
            python_routes: AtomicBool::new(false),
        });
        let (shutdown_tx, shutdown_rx) = oneshot::channel::<()>();
        let state = shared.clone();
        let thread = std::thread::Builder::new()
            .name("mediasrv".into())
            .spawn(move || run_server(listener, state, shutdown_rx))
            .map_err(|e| PyException::new_err(e.to_string()))?;
        Ok(Self {
            shared,
            port,
            host,
            shutdown: Some(shutdown_tx),
            thread: Some(thread),
        })
    }

    #[getter]
    fn port(&self) -> u16 {
        self.port
    }

    #[getter]
    fn host(&self) -> &str {
        &self.host
    }

    fn set_csp(
        &self,
        trusted_page: String,
        untrusted_media: String,
        legacy_editor: String,
        untrusted_sveltekit: String,
    ) {
        *self.shared.csp.write().unwrap() = Csp {
            trusted_page,
            untrusted_media,
            legacy_editor,
            untrusted_sveltekit,
        };
    }

    #[pyo3(signature = (path=None))]
    fn set_media_dir(&self, path: Option<PathBuf>) {
        *self.shared.media_dir.write().unwrap() = path;
    }

    /// Pass media paths to Python first, for hooks.media_file_filter.
    fn set_filter_media_paths(&self, enabled: bool) {
        self.shared
            .filter_media_paths
            .store(enabled, Ordering::Relaxed);
    }

    /// Let Python handle requests that would otherwise be served as media,
    /// for routes that add-ons added to aqt.mediasrv.app.
    fn set_python_routes(&self, enabled: bool) {
        self.shared.python_routes.store(enabled, Ordering::Relaxed);
    }

    /// Returns false if Python has to match the pattern.
    #[pyo3(signature = (addon, pattern=None))]
    fn set_web_exports(&self, addon: String, pattern: Option<&str>) -> bool {
        let mut exports = self.shared.web_exports.write().unwrap();
        let Some(pattern) = pattern else {
            exports.remove(&addon);
            return true;
        };
        // Python's re.fullmatch()
        match Regex::new(&format!("^(?:{pattern})$")) {
            Ok(re) => {
                exports.insert(addon, WebExport::Regex(re));
                true
            }
            Err(_) => {
                exports.insert(addon, WebExport::Python);
                false
            }
        }
    }

    fn set_page(&self, id: u64, html: String, context: u8) {
        self.shared
            .pages
            .write()
            .unwrap()
            .insert(id, Page { html, context });
    }

    fn clear_page(&self, id: u64) {
        self.shared.pages.write().unwrap().remove(&id);
    }

    fn shutdown(&mut self, py: Python) {
        if let Some(tx) = self.shutdown.take() {
            let _ = tx.send(());
        }
        if let Some(thread) = self.thread.take() {
            // Python requests in flight need the GIL to finish
            py.detach(|| {
                let _ = thread.join();
            });
        }
    }
}

fn run_server(listener: StdTcpListener, shared: Arc<Shared>, shutdown: oneshot::Receiver<()>) {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_name("mediasrv-worker")
        .enable_all()
        .build()
        .expect("tokio runtime");
    runtime.block_on(async move {
        let listener = tokio::net::TcpListener::from_std(listener).expect("listener");
        let app = Router::new().fallback(handle).with_state(shared);
        let _ = axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                let _ = shutdown.await;
            })
            .await;
    });
    runtime.shutdown_background();
}

// Request handling
//////////////////////////////////

async fn handle(State(shared): State<Arc<Shared>>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    if shared.csp.read().unwrap().trusted_page.is_empty() {
        // never serve pages without their Content-Security-Policy
        return text_response(StatusCode::SERVICE_UNAVAILABLE, "not ready");
    }
    if !shared.allow_any_host {
        if let Err(response) = check_host_and_origin(&parts.headers) {
            return response;
        }
    }
    let raw_path = parts.uri.path().trim_start_matches('/');
    let path = match percent_decode_str(raw_path).decode_utf8() {
        Ok(path) => path.into_owned(),
        Err(_) => return text_response(StatusCode::BAD_REQUEST, "invalid path encoding"),
    };
    let query = parts.uri.query().unwrap_or_default().to_string();

    match route(&shared, &parts.method, &path) {
        Route::Bundled { path, sveltekit } => serve_bundled(&shared, &path, sveltekit),
        Route::LegacyPage => serve_legacy_page(&shared, &query),
        Route::Python => {
            let body = axum::body::to_bytes(body, usize::MAX)
                .await
                .unwrap_or_default();
            call_python(shared, parts.method, path, query, parts.headers, body).await
        }
        Route::AddonFile(addon_path) => {
            let root = shared.addons_root.clone();
            serve_local_file(&root, &addon_path, None, parts).await
        }
        Route::MatchWebExportInPython { addon, sub_path } => {
            let python = Python::attach(|py| shared.python.clone_ref(py));
            let matched = tokio::task::spawn_blocking(move || {
                Python::attach(|py| {
                    python
                        .call_method1(py, "match_web_export", (addon, sub_path))
                        .and_then(|res| res.extract::<bool>(py))
                        .unwrap_or(false)
                })
            })
            .await
            .unwrap_or(false);
            if matched {
                let root = shared.addons_root.clone();
                serve_local_file(&root, &path["_addons/".len()..], None, parts).await
            } else {
                not_found(&path)
            }
        }
        Route::Media => serve_media(shared, path, query, parts, body).await,
        Route::NotFound => not_found(&path),
    }
}

enum Route {
    Bundled {
        path: String,
        sveltekit: Option<&'static str>,
    },
    LegacyPage,
    Python,
    AddonFile(String),
    MatchWebExportInPython {
        addon: String,
        sub_path: String,
    },
    Media,
    NotFound,
}

fn route(shared: &Shared, method: &Method, path: &str) -> Route {
    if path == "favicon.ico" {
        return Route::Bundled {
            path: "imgs/favicon.ico".into(),
            sveltekit: None,
        };
    }
    if let Some(route) = route_internal(method, path) {
        return route;
    }
    if let Some(route) = route_addon(shared, path) {
        return route;
    }
    Route::Media
}

fn sveltekit_route(path: &str) -> Option<&'static str> {
    let page = path.split('/').next().unwrap_or_default();
    SVELTEKIT_ROUTES.iter().find(|r| **r == page).copied()
}

/// /_anki/ requests, SvelteKit routes and their /_app/ files.
fn route_internal(method: &Method, path: &str) -> Option<Route> {
    let sveltekit = sveltekit_route(path);
    let path = if sveltekit.is_some() {
        format!("_anki/sveltekit/_app/{path}")
    } else if let Some(rest) = path.strip_prefix("_app/") {
        format!("_anki/sveltekit/_app/{rest}")
    } else {
        path.to_string()
    };
    let rest = path.strip_prefix("_anki/")?;
    let (dirname, filename) = match rest.rsplit_once('/') {
        Some((dir, file)) => (format!("_anki/{dir}"), file),
        None => ("_anki".to_string(), rest),
    };

    let mut remapped = None;
    if dirname == "_anki" {
        if method == Method::POST {
            return Some(Route::Python);
        }
        match filename {
            "legacyPageData" => return Some(Route::LegacyPage),
            "readyz" => return Some(Route::Python),
            _ => {}
        }
        // legacy top-level references
        let (base, ext) = split_ext(filename);
        remapped = match ext {
            ".css" => Some(format!("css/{base}{ext}")),
            ".js" if matches!(base, "jquery-ui" | "jquery" | "plot") => {
                Some(format!("js/vendor/{base}{ext}"))
            }
            ".js" => Some(format!("js/{base}{ext}")),
            _ => None,
        };
    } else if dirname == "_anki/js/vendor" {
        let (base, ext) = split_ext(filename);
        remapped = match base {
            "jquery" => Some(format!("js/vendor/jquery.min{ext}")),
            "jquery-ui" => Some(format!("js/vendor/jquery-ui.min{ext}")),
            _ => None,
        };
    }
    Some(Route::Bundled {
        path: remapped.unwrap_or_else(|| rest.to_string()),
        sveltekit,
    })
}

/// /_addons/<addon>/<path>, for files that add-ons export with setWebExports().
fn route_addon(shared: &Shared, path: &str) -> Option<Route> {
    let addon_path = path.strip_prefix("_addons/")?;
    let (addon, sub_path) = addon_path.split_once('/')?;
    if addon.is_empty() {
        return None;
    }
    let exports = shared.web_exports.read().unwrap();
    match exports.get(addon)? {
        WebExport::Regex(re) if re.is_match(sub_path) => {
            Some(Route::AddonFile(addon_path.to_string()))
        }
        WebExport::Regex(_) => Some(Route::NotFound),
        WebExport::Python => Some(Route::MatchWebExportInPython {
            addon: addon.to_string(),
            sub_path: sub_path.to_string(),
        }),
    }
}

fn split_ext(filename: &str) -> (&str, &str) {
    match filename.rfind('.') {
        Some(idx) if idx > 0 => filename.split_at(idx),
        _ => (filename, ""),
    }
}

fn check_host_and_origin(headers: &HeaderMap) -> Result<(), Response> {
    let host = header_str(headers, header::HOST).to_ascii_lowercase();
    if !LOCALHOST_HOSTS
        .iter()
        .any(|h| host.starts_with(&format!("{h}:")))
    {
        return Err(text_response(StatusCode::FORBIDDEN, "Forbidden"));
    }
    let origin = header_str(headers, header::ORIGIN).to_ascii_lowercase();
    if !origin.is_empty() && !is_localhost_origin(&origin) {
        return Err(text_response(StatusCode::FORBIDDEN, "Forbidden"));
    }
    Ok(())
}

/// Must match is_localhost_origin() in aqt.mediasrv.
fn is_localhost_origin(origin: &str) -> bool {
    ["http://", "https://"].iter().any(|scheme| {
        LOCALHOST_HOSTS.iter().any(|host| {
            let prefix = format!("{scheme}{host}");
            origin == prefix
                || origin.starts_with(&format!("{prefix}:"))
                || origin.starts_with(&format!("{prefix}/"))
        })
    })
}

fn header_str(headers: &HeaderMap, name: header::HeaderName) -> &str {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default()
}

// Responses
//////////////////////////////////

/// A plain text response, so that untrusted input needs no escaping.
fn text_response(status: StatusCode, text: &str) -> Response {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "text/plain")
        .body(Body::from(text.to_string()))
        .unwrap()
}

fn not_found(path: &str) -> Response {
    text_response(StatusCode::NOT_FOUND, &format!("Invalid path: {path}"))
}

fn set_header(response: &mut Response, name: header::HeaderName, value: &str) {
    if let Ok(value) = HeaderValue::from_str(value) {
        response.headers_mut().insert(name, value);
    }
}

/// Must match _mime_for_path() in aqt.mediasrv. Hard-coded, because badly
/// behaved apps on Windows can change the registry's mime types.
fn mime_for_path(path: &str) -> String {
    let ext = Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    let mime = match ext.as_str() {
        "css" => "text/css",
        "js" | "mjs" => "application/javascript",
        "html" | "htm" => "text/html",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "ico" => "image/x-icon",
        "json" => "application/json",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "otf" => "font/otf",
        "mp3" => "audio/mpeg",
        "mp4" => "video/mp4",
        "webm" => "video/webm",
        "ogg" => "audio/ogg",
        "pdf" => "application/pdf",
        "txt" => "text/plain",
        _ => {
            return mime_guess::from_path(path)
                .first_raw()
                .unwrap_or("application/octet-stream")
                .to_string()
        }
    };
    mime.to_string()
}

/// Joins a relative path to a base folder, refusing anything that could
/// escape it. Must match ensure_safe_path() in aqt.mediasrv.
fn safe_join(base: &Path, path: &str) -> Option<PathBuf> {
    let mut full = base.to_path_buf();
    let mut parts = 0;
    for component in Path::new(path).components() {
        match component {
            Component::Normal(part) => {
                full.push(part);
                parts += 1;
            }
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => return None,
        }
    }
    (parts > 0).then_some(full)
}

fn serve_bundled(shared: &Shared, path: &str, sveltekit: Option<&str>) -> Response {
    let immutable = path.contains("immutable");
    let path = if path.starts_with("sveltekit/") && !immutable {
        "sveltekit/index.html"
    } else {
        path
    };
    let Some(full_path) = safe_join(&shared.web_root, path) else {
        return text_response(StatusCode::FORBIDDEN, &format!("Invalid path: {path}"));
    };
    let data = match std::fs::read(&full_path) {
        Ok(data) => data,
        Err(_) => return not_found(path),
    };
    let mime = mime_for_path(path);
    let csp = shared.csp.read().unwrap().clone();
    let mut csp_header = None;
    let mut data = data;
    if let Some(route) = sveltekit {
        let is_index = path.ends_with("index.html");
        if UNTRUSTED_SVELTEKIT_ROUTES.contains(&route) {
            let hash = if is_index {
                sveltekit_render_script_hash(&data)
            } else {
                None
            };
            csp_header = Some(untrusted_sveltekit_csp(&csp, hash.as_deref()));
        } else if is_index {
            match strip_csp_meta(&data) {
                Some(stripped) => data = stripped,
                None => {
                    return text_response(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        "CSP meta tag not found in SvelteKit index page",
                    )
                }
            }
            csp_header = Some(csp.trusted_page.clone());
        }
    } else if mime == "text/html" {
        csp_header = Some(csp.trusted_page.clone());
    }

    let mut response = Response::new(Body::from(data));
    set_header(&mut response, header::CONTENT_TYPE, &mime);
    if immutable {
        set_header(&mut response, header::CACHE_CONTROL, "max-age=31536000");
    }
    if let Some(csp) = csp_header {
        set_header(&mut response, header::CONTENT_SECURITY_POLICY, &csp);
    }
    response
}

fn untrusted_sveltekit_csp(csp: &Csp, hash: Option<&str>) -> String {
    let hash = hash.map(|h| format!(" {h}")).unwrap_or_default();
    csp.untrusted_sveltekit.replace(" {hash}", &hash)
}

fn csp_meta_regex() -> &'static Regex {
    static RE: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r#"<meta http-equiv="content-security-policy" content="script-src 'self' ('sha256-[A-Za-z0-9+/=]+')">"#,
        )
        .unwrap()
    })
}

/// The hash SvelteKit computed for its inline render script.
fn sveltekit_render_script_hash(html: &[u8]) -> Option<String> {
    let html = std::str::from_utf8(html).ok()?;
    csp_meta_regex()
        .captures(html)
        .map(|caps| caps[1].to_string())
}

fn strip_csp_meta(html: &[u8]) -> Option<Vec<u8>> {
    let html = std::str::from_utf8(html).ok()?;
    if !csp_meta_regex().is_match(html) {
        return None;
    }
    Some(
        csp_meta_regex()
            .replace_all(html, "")
            .into_owned()
            .into_bytes(),
    )
}

fn serve_legacy_page(shared: &Shared, query: &str) -> Response {
    let id = query
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .find(|(key, _)| *key == "id")
        .and_then(|(_, value)| value.parse::<u64>().ok());
    let pages = shared.pages.read().unwrap();
    let Some(page) = id.and_then(|id| pages.get(&id)) else {
        return text_response(StatusCode::NOT_FOUND, "page not found");
    };
    let csp = shared.csp.read().unwrap();
    let mut response = Response::new(Body::from(page.html.clone()));
    set_header(&mut response, header::CONTENT_TYPE, "text/html");
    // JS in field content must not run in the editor, as it would have access
    // to our internal API
    let policy = if page.context == PAGE_CONTEXT_EDITOR {
        &csp.legacy_editor
    } else {
        &csp.trusted_page
    };
    set_header(&mut response, header::CONTENT_SECURITY_POLICY, policy);
    response
}

async fn serve_media(
    shared: Arc<Shared>,
    path: String,
    query: String,
    parts: axum::http::request::Parts,
    body: Body,
) -> Response {
    if shared.python_routes.load(Ordering::Relaxed) {
        let body = axum::body::to_bytes(body, usize::MAX)
            .await
            .unwrap_or_default();
        let response = call_python(
            shared.clone(),
            parts.method.clone(),
            path.clone(),
            query,
            parts.headers.clone(),
            body,
        )
        .await;
        if !response.headers().contains_key("x-anki-not-handled") {
            return response;
        }
    }
    let Some(media_dir) = shared.media_dir.read().unwrap().clone() else {
        // collection not open
        return not_found(&path);
    };
    let path = if shared.filter_media_paths.load(Ordering::Relaxed) {
        let python = Python::attach(|py| shared.python.clone_ref(py));
        let original = path.clone();
        tokio::task::spawn_blocking(move || {
            Python::attach(|py| {
                python
                    .call_method1(py, "filter_media_path", (original.clone(),))
                    .and_then(|res| res.extract::<String>(py))
                    .unwrap_or(original)
            })
        })
        .await
        .unwrap_or(path)
    } else {
        path
    };
    let csp = shared.csp.read().unwrap().untrusted_media.clone();
    serve_local_file(&media_dir, &path, Some(csp), parts).await
}

/// A file from the media folder (untrusted user content) or an add-on folder.
async fn serve_local_file(
    root: &Path,
    path: &str,
    untrusted_csp: Option<String>,
    parts: axum::http::request::Parts,
) -> Response {
    let Some(full_path) = safe_join(root, path) else {
        return text_response(StatusCode::FORBIDDEN, &format!("Invalid path: {path}"));
    };
    if full_path.is_dir() {
        return text_response(
            StatusCode::FORBIDDEN,
            &format!("Path for '{path}' is a directory (not supported)!"),
        );
    }
    if !full_path.is_file() {
        return not_found(path);
    }
    let mime = mime_for_path(path);
    // ServeFile handles Range and If-Modified-Since requests
    let request = Request::from_parts(parts, Body::empty());
    let mut response = match ServeFile::new(&full_path).try_call(request).await {
        Ok(response) => response.map(Body::new),
        Err(err) => return text_response(StatusCode::INTERNAL_SERVER_ERROR, &err.to_string()),
    };
    set_header(&mut response, header::CONTENT_TYPE, &mime);
    let max_age = if path.ends_with(".css") {
        // caching css prevents flicker in the webview, but keep it short
        "max-age=10"
    } else if path.ends_with(".js") {
        "max-age=0"
    } else {
        "max-age=3600"
    };
    set_header(&mut response, header::CACHE_CONTROL, max_age);
    if let Some(csp) = untrusted_csp {
        // user-provided HTML/SVG must not run as an active document
        set_header(&mut response, header::CONTENT_SECURITY_POLICY, &csp);
    }
    response
}

async fn call_python(
    shared: Arc<Shared>,
    method: Method,
    path: String,
    query: String,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let headers: Vec<(String, String)> = headers
        .iter()
        .filter_map(|(k, v)| Some((k.as_str().to_string(), v.to_str().ok()?.to_string())))
        .collect();
    let result = tokio::task::spawn_blocking(move || {
        Python::attach(|py| -> PyResult<(u16, Vec<(String, String)>, Vec<u8>)> {
            let body = PyBytes::new(py, &body);
            shared
                .python
                .call_method1(py, "handle", (method.as_str(), path, query, headers, body))?
                .extract(py)
        })
    })
    .await;
    match result {
        Ok(Ok((status, headers, body))) => {
            let mut response = Response::new(Body::from(body));
            *response.status_mut() =
                StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
            for (name, value) in headers {
                if let (Ok(name), Ok(value)) = (
                    header::HeaderName::from_bytes(name.as_bytes()),
                    HeaderValue::from_str(&value),
                ) {
                    response.headers_mut().append(name, value);
                }
            }
            response
        }
        Ok(Err(err)) => text_response(StatusCode::INTERNAL_SERVER_ERROR, &err.to_string()),
        Err(err) => text_response(StatusCode::INTERNAL_SERVER_ERROR, &err.to_string()),
    }
}
