/** Bounded CDP transport for an already-running, personally logged-in browser.
 * No browser launch, profile copy, cookie API, daemon, or arbitrary CLI script.
 * Only repository-owned readers may supply the callback/evaluation function.
 */
import { randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';

export class BrowserCdpError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'BrowserCdpError';
    this.code = code;
  }
}

const error = (code, message) => new BrowserCdpError(code, message);
const MAX_INPUT = 64 * 1024;
const CLEANUP_MS = 3000;

function boundedInteger(value, fallback, minimum, maximum, name) {
  const result = value === undefined ? fallback : value;
  if (!Number.isInteger(result) || result < minimum || result > maximum) {
    throw error('browser_invalid_config', `Invalid ${name}.`);
  }
  return result;
}

function parseURL(value) {
  try {
    if (typeof value !== 'string' || !value || value !== value.trim()) throw 0;
    return new URL(value);
  } catch {
    throw error('browser_invalid_config', 'Invalid browser URL.');
  }
}

function endpointURL(value) {
  const url = parseURL(value);
  if (url.protocol !== 'http:' || url.username || url.password || url.search || url.hash ||
      url.pathname !== '/' || !['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname) || !url.port) {
    throw error('browser_invalid_config', 'CDP must use an explicit local loopback HTTP port.');
  }
  // Do not resolve localhost through DNS or a changed hosts file.
  if (url.hostname === 'localhost') url.hostname = '127.0.0.1';
  return url;
}

function pageURL(value, origin, pathname) {
  const url = parseURL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.origin !== origin ||
      (pathname !== undefined && url.pathname !== pathname)) {
    throw error('browser_navigation_blocked', 'Only the configured Base page may be opened.');
  }
  return url;
}

export function validateBrowserOptions(options) {
  if (!options || typeof options !== 'object' || Array.isArray(options)) {
    throw error('browser_invalid_config', 'Browser settings are missing.');
  }
  const endpoint = endpointURL(options.cdp_endpoint);
  const originURL = parseURL(options.origin);
  if (originURL.protocol !== 'https:' || originURL.username || originURL.password ||
      originURL.pathname !== '/' || originURL.search || originURL.hash) {
    throw error('browser_invalid_config', 'A single HTTPS binding origin is required.');
  }
  const url = pageURL(options.url, originURL.origin);
  return {
    endpoint, origin: originURL.origin, url, pathname: url.pathname,
    timeout_ms: boundedInteger(options.timeout_ms, 30000, 100, 300000, 'timeout_ms'),
    max_result_bytes: boundedInteger(options.max_result_bytes, 1024 * 1024, 128, 8 * 1024 * 1024,
      'max_result_bytes'),
  };
}

async function readBoundedJSON(response, limit) {
  if (!response.ok || !response.body) {
    throw error('browser_connect_failed', 'The local browser did not return CDP connection details.');
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > limit) throw error('browser_response_too_large', 'CDP connection details exceed the limit.');
      chunks.push(Buffer.from(value));
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch (cause) {
    await reader.cancel().catch(() => {});
    if (cause instanceof BrowserCdpError) throw cause;
    throw error('browser_connect_failed', 'The local browser returned invalid CDP connection details.');
  } finally {
    reader.releaseLock();
  }
}

async function connect(options, signal, onFailure) {
  let metadata;
  try {
    metadata = await readBoundedJSON(await fetch(new URL('/json/version', options.endpoint), {
      redirect: 'error', signal,
    }), MAX_INPUT);
  } catch (cause) {
    if (cause instanceof BrowserCdpError) throw cause;
    throw error('browser_connect_failed', 'Cannot connect to the configured local browser.');
  }
  const wsURL = parseURL(metadata.webSocketDebuggerUrl);
  if (wsURL.protocol !== 'ws:' || wsURL.username || wsURL.password || wsURL.search || wsURL.hash ||
      wsURL.hostname !== options.endpoint.hostname || wsURL.port !== options.endpoint.port ||
      !/^\/devtools\/browser\/[^/]+$/.test(wsURL.pathname)) {
    throw error('browser_connect_failed', 'CDP returned an unexpected browser connection address.');
  }
  let socket;
  try { socket = new WebSocket(wsURL); } catch {
    throw error('browser_connect_failed', 'Cannot open the local browser connection.');
  }
  const pending = new Map();
  let nextID = 0;
  let closing = false;
  let eventHandler = () => {};
  const rejectPending = (failure) => {
    for (const call of pending.values()) {
      clearTimeout(call.timer);
      call.reject(failure);
    }
    pending.clear();
  };
  socket.addEventListener('message', (event) => {
    try {
      if (typeof event.data !== 'string' || Buffer.byteLength(event.data) > options.max_result_bytes + MAX_INPUT) {
        throw error('browser_response_too_large', 'The browser response exceeds the configured limit.');
      }
      const message = JSON.parse(event.data);
      if (Object.hasOwn(message, 'id')) {
        const call = pending.get(message.id);
        if (!call) return;
        pending.delete(message.id);
        clearTimeout(call.timer);
        if (message.error) call.reject(error('browser_protocol_error', 'The browser rejected a read operation.'));
        else call.resolve(message.result ?? {});
      } else {
        Promise.resolve(eventHandler(message)).catch(onFailure);
      }
    } catch (cause) {
      const failure = cause instanceof BrowserCdpError ? cause :
        error('browser_protocol_error', 'The browser returned an invalid protocol message.');
      rejectPending(failure);
      onFailure(failure);
    }
  });
  socket.addEventListener('close', () => {
    const failure = error('browser_connection_closed', 'The browser connection closed during the read.');
    rejectPending(failure);
    if (!closing) onFailure(failure);
  });
  socket.addEventListener('error', () => {
    if (!closing) onFailure(error('browser_connect_failed', 'The local browser connection failed.'));
  });

  try {
    await new Promise((resolve, reject) => {
      const abort = () => {
        socket.close();
        reject(error('browser_timeout', 'The browser connection timed out.'));
      };
      if (signal.aborted) return abort();
      signal.addEventListener('abort', abort, { once: true });
      socket.addEventListener('open', () => {
        signal.removeEventListener('abort', abort);
        resolve();
      }, { once: true });
      socket.addEventListener('error', () => {
        signal.removeEventListener('abort', abort);
        reject(error('browser_connect_failed', 'Cannot open the local browser connection.'));
      }, { once: true });
    });
  } catch (cause) {
    closing = true;
    socket.close();
    throw cause;
  }

  return {
    setEventHandler(handler) { eventHandler = handler; },
    command(method, params = {}, sessionId, timeout = options.timeout_ms) {
      if (socket.readyState !== WebSocket.OPEN) {
        return Promise.reject(error('browser_connection_closed', 'The local browser connection is unavailable.'));
      }
      const id = ++nextID;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(error('browser_timeout', 'The browser read timed out.'));
        }, timeout);
        pending.set(id, { resolve, reject, timer });
        try {
          socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
        } catch {
          clearTimeout(timer);
          pending.delete(id);
          reject(error('browser_connection_closed', 'The browser connection is unavailable.'));
        }
      });
    },
    close() {
      closing = true;
      rejectPending(error('browser_connection_closed', 'The browser read has ended.'));
      socket.close();
    },
  };
}

/** Open exactly one temporary tab in the existing default browser context.
 * The callback is trusted repository code, never JavaScript from stdin, Base
 * cells, comments, or configuration. It must not access credentials/storage.
 * evaluate(fn, arg) serializes one function and JSON data; closures are absent.
 * A short-lived CLI caller should flush its result and explicitly exit after
 * this returns: WebSocket.close can wait indefinitely for a peer's handshake.
 */
export async function withOwnedPage(rawOptions, readPage) {
  const options = validateBrowserOptions(rawOptions);
  if (typeof readPage !== 'function') throw error('browser_invalid_config', 'A fixed page reader is required.');
  if (Number(process.versions.node.split('.')[0]) < 22 || typeof WebSocket !== 'function') {
    throw error('browser_node_required', 'Browser reads require Node.js 22 or newer.');
  }
  const controller = new AbortController();
  const deadline = Date.now() + options.timeout_ms;
  const marker = `about:blank#lark-dev-workflow-${randomUUID()}`;
  let client, targetID, sessionID, mainFrameID, creationStarted = false, navigating = false;
  const loadedDocuments = new Set();
  let failure, cleanupFailure, finished = false;
  let rejectFailure;
  const failed = new Promise((_, reject) => { rejectFailure = reject; });
  // A connection failure can arrive before the next operation starts racing it.
  failed.catch(() => {});
  const stop = (cause) => {
    if (finished || failure) return;
    failure = cause instanceof BrowserCdpError ? cause :
      error('browser_read_failed', 'The browser page could not be read.');
    controller.abort();
    rejectFailure(failure);
  };
  const timeout = setTimeout(() => stop(error('browser_timeout', 'The browser read timed out.')), options.timeout_ms);
  const interrupted = () => stop(error('browser_interrupted', 'The browser read was interrupted.'));
  process.once('SIGTERM', interrupted);
  process.once('SIGINT', interrupted);
  const command = (method, params = {}, session = sessionID) => Promise.race([
    client.command(method, params, session, Math.max(1, deadline - Date.now())), failed,
  ]);
  const allowed = (url) => {
    try { pageURL(url, options.origin, options.pathname); return true; } catch { return false; }
  };
  const blockedNavigation = () => error('browser_navigation_blocked',
    'The browser tried to leave the configured Base page. Sign in or check access in your existing browser.');
  let result;
  try {
    client = await connect(options, controller.signal, stop);
    creationStarted = true;
    const created = await command('Target.createTarget', { url: marker, background: true }, undefined);
    if (typeof created.targetId !== 'string' || !created.targetId) {
      throw error('browser_protocol_error', 'The browser did not identify the temporary tab.');
    }
    targetID = created.targetId;
    const attached = await command('Target.attachToTarget', { targetId: targetID, flatten: true }, undefined);
    if (typeof attached.sessionId !== 'string' || !attached.sessionId) {
      throw error('browser_protocol_error', 'The browser did not attach the temporary tab.');
    }
    sessionID = attached.sessionId;
    client.setEventHandler(async (message) => {
      if (message.sessionId !== sessionID || finished) return;
      if (message.method === 'Page.lifecycleEvent' &&
          ['DOMContentLoaded', 'load'].includes(message.params?.name)) {
        loadedDocuments.add(message.params.loaderId);
        if (loadedDocuments.size > 64) loadedDocuments.delete(loadedDocuments.values().next().value);
      }
      if (message.method === 'Fetch.requestPaused') {
        const params = message.params ?? {};
        // Only document navigations are intercepted. Ordinary page assets keep
        // browser behavior; no response bodies/headers/cookies are collected.
        if (typeof params.frameId !== 'string' || !params.frameId) {
          await client.command('Fetch.failRequest', { requestId: params.requestId, errorReason: 'BlockedByClient' },
            sessionID, Math.max(1, deadline - Date.now()));
          stop(error('browser_protocol_error', 'The browser did not identify the navigating frame.'));
        } else if (params.frameId !== mainFrameID || allowed(params.request?.url)) {
          // Embedded frames are part of the normal page. Do not navigate or
          // inspect them, and do not block their own document requests.
          await client.command('Fetch.continueRequest', { requestId: params.requestId }, sessionID,
            Math.max(1, deadline - Date.now()));
        } else {
          await client.command('Fetch.failRequest', { requestId: params.requestId, errorReason: 'BlockedByClient' },
            sessionID, Math.max(1, deadline - Date.now()));
          stop(blockedNavigation());
        }
      }
      if (navigating && message.method === 'Page.frameNavigated' &&
          message.params?.frame?.id === mainFrameID &&
          !allowed(message.params?.frame?.url)) stop(blockedNavigation());
      if (navigating && message.method === 'Page.navigatedWithinDocument' &&
          message.params?.frameId === mainFrameID &&
          !allowed(message.params?.url)) stop(blockedNavigation());
    });
    await command('Page.enable');
    const frames = await command('Page.getFrameTree');
    mainFrameID = frames.frameTree?.frame?.id;
    if (typeof mainFrameID !== 'string' || !mainFrameID) {
      throw error('browser_protocol_error', 'The browser did not identify the main frame.');
    }
    await command('Page.setLifecycleEventsEnabled', { enabled: true });
    await command('Fetch.enable', { patterns: [{ urlPattern: '*', resourceType: 'Document', requestStage: 'Request' }] });
    const navigate = async (url) => {
      const target = pageURL(url, options.origin, options.pathname);
      navigating = true;
      const response = await command('Page.navigate', { url: target.href });
      if (response.errorText || response.isDownload) {
        throw failure ?? error('browser_navigation_failed', 'The configured Base page could not be opened.');
      }
      // navigate acknowledges the start, not DOM readiness. Wait for the
      // specific new document; fixed readers can then await their own grid.
      if (response.loaderId) {
        while (!loadedDocuments.has(response.loaderId)) {
          await Promise.race([new Promise((resolve) => setTimeout(resolve, 20)), failed]);
        }
      }
    };
    await navigate(options.url.href);
    const page = Object.freeze({
      url: options.url.href,
      navigate,
      async evaluate(fn, arg = null) {
        if (typeof fn !== 'function') throw error('browser_invalid_reader', 'Page evaluations require a fixed function.');
        let expression;
        try {
          // Check the live execution context as well as navigation events;
          // never run the fixed reader in an old or unexpected document.
          expression = `(async () => {
            if (location.protocol !== 'https:' || location.origin !== ${JSON.stringify(options.origin)} ||
                location.pathname !== ${JSON.stringify(options.pathname)}) return { guard: false };
            return { guard: true, value: await (${fn.toString()})(${JSON.stringify(arg)}) };
          })()`;
        } catch {
          throw error('browser_invalid_reader', 'The page reader argument is invalid.');
        }
        if (Buffer.byteLength(expression) > MAX_INPUT) {
          throw error('browser_invalid_reader', 'The fixed page reader exceeds the size limit.');
        }
        const response = await command('Runtime.evaluate', {
          expression, returnByValue: true, awaitPromise: true, userGesture: false,
          timeout: Math.max(1, deadline - Date.now()),
        });
        if (response.exceptionDetails) throw error('browser_page_error', 'The fixed page reader failed.');
        const envelope = response.result?.value;
        if (envelope?.guard === false) throw blockedNavigation();
        if (envelope?.guard !== true || !Object.hasOwn(envelope, 'value')) {
          throw error('browser_page_error', 'The fixed page reader returned no data.');
        }
        const value = envelope.value;
        let encoded;
        try { encoded = JSON.stringify(value); } catch {
          throw error('browser_page_error', 'The fixed page reader returned invalid data.');
        }
        if (encoded === undefined) throw error('browser_page_error', 'The fixed page reader returned no data.');
        if (Buffer.byteLength(encoded) > options.max_result_bytes) {
          throw error('browser_response_too_large', 'The browser result exceeds the configured limit.');
        }
        return value;
      },
    });
    result = await Promise.race([Promise.resolve().then(() => readPage(page)), failed]);
    const encoded = JSON.stringify(result);
    if (encoded === undefined || Buffer.byteLength(encoded) > options.max_result_bytes) {
      throw error('browser_response_too_large', 'The browser result exceeds the configured limit.');
    }
    if (failure) throw failure;
  } catch (cause) {
    failure ??= cause instanceof BrowserCdpError ? cause : error('browser_read_failed', 'The browser page could not be read.');
  } finally {
    finished = true;
    clearTimeout(timeout);
    controller.abort();
    if (client && creationStarted) {
      try {
        // If createTarget's reply was lost, recover only our unpredictable
        // about:blank marker. Never close an existing tab by title or position.
        if (!targetID) {
          const targets = await client.command('Target.getTargets', {}, undefined, CLEANUP_MS);
          const matches = (targets.targetInfos ?? []).filter((t) => t.type === 'page' && t.url === marker);
          if (matches.length !== 1) throw 0;
          targetID = matches[0].targetId;
        }
        const closed = await client.command('Target.closeTarget', { targetId: targetID }, undefined, CLEANUP_MS);
        if (closed.success !== true) throw 0;
      } catch {
        cleanupFailure = error('browser_cleanup_failed',
          'Could not confirm that the temporary browser tab closed. The read was not completed.');
      }
    }
    client?.close();
    process.removeListener('SIGTERM', interrupted);
    process.removeListener('SIGINT', interrupted);
  }
  if (cleanupFailure) throw cleanupFailure;
  if (failure) throw failure;
  return result;
}

async function main() {
  let raw = '';
  process.stdin.setEncoding('utf8');
  for await (const chunk of process.stdin) {
    raw += chunk;
    if (Buffer.byteLength(raw) > MAX_INPUT) throw error('browser_invalid_config', 'Browser input exceeds the limit.');
  }
  let input;
  try { input = JSON.parse(raw); } catch { throw error('browser_invalid_config', 'Browser input must be JSON.'); }
  if (Object.keys(input).some((key) => !['cdp_endpoint', 'origin', 'url', 'timeout_ms', 'max_result_bytes'].includes(key))) {
    throw error('browser_invalid_config', 'The connection probe does not accept scripts or extra options.');
  }
  const data = await withOwnedPage(input, (page) => page.evaluate(() => ({
    url: location.href, title: document.title, ready_state: document.readyState,
  })));
  return { ok: true, data };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const finish = (value, code) => {
    // This is only the dedicated probe subprocess, never a library caller.
    // withOwnedPage has already completed its exact-target cleanup attempt.
    process.stdout.write(JSON.stringify(value) + '\n', () => process.exit(code));
  };
  main().then((value) => finish(value, 0)).catch((cause) => {
    const failure = cause instanceof BrowserCdpError ? cause : error('browser_read_failed', 'The browser read failed.');
    finish({ ok: false, error: { code: failure.code, message: failure.message } }, 1);
  });
}
