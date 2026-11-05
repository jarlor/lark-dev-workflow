/** Fixed read-only Base collector. Browser credentials never leave Chrome. */
import { withOwnedPage, BrowserCdpError } from './browser_cdp.mjs';
import { readTable, readComments } from './browser_page.mjs';
import { pathToFileURL } from 'node:url';

const failure = (code) => ({ status: 'failed', code });
const encode = (value) => JSON.stringify(value, (_key, item) =>
  item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);

export async function collect(input, withPage = withOwnedPage) {
  if (Number(process.versions.node.split('.')[0]) < 22) return failure('NODE_VERSION');
  if (!input || typeof input !== 'object' || Object.keys(input).sort().join(',') !== 'base,cdp_url,tables,user_id' ||
      typeof input.user_id !== 'string' || !input.user_id || !input.base || typeof input.base.token !== 'string' ||
      !Array.isArray(input.tables) || !input.tables.length || input.tables.length > 32) return failure('INVALID_INPUT');
  let url;
  try { url = new URL(input.base.url); } catch { return failure('INVALID_INPUT'); }
  if (url.protocol !== 'https:' || url.username || url.password ||
      !/(^|\.)(feishu\.cn|larksuite\.com)$/.test(url.hostname) ||
      url.pathname !== '/base/' + input.base.token) return failure('INVALID_INPUT');
  const ids = new Set();
  for (const table of input.tables) {
    if (!table || typeof table.id !== 'string' || !table.id || ids.has(table.id) ||
        !Array.isArray(table.field_ids) || !table.field_ids.length || table.field_ids.length > 256 ||
        table.field_ids.some((id) => typeof id !== 'string' || !id) ||
        new Set(table.field_ids).size !== table.field_ids.length) return failure('INVALID_INPUT');
    ids.add(table.id);
  }
  const safeURL = new URL(url.origin + url.pathname);
  safeURL.searchParams.set('table', input.tables[0].id);
  return withPage({ cdp_endpoint: input.cdp_url, origin: url.origin, url: safeURL.href,
    timeout_ms: 300000, max_result_bytes: 8 * 1024 * 1024 }, async (page) => {
    const readPass = async (pass) => {
      const tables = {};
      for (const [index, table] of input.tables.entries()) {
        if (index || pass) {
          safeURL.searchParams.set('table', table.id);
          await page.navigate(safeURL.href);
        }
        const result = await page.evaluate(readTable, { base_token: input.base.token,
          user_id: input.user_id, table_id: table.id, field_ids: table.field_ids });
        if (result?.status !== 'complete') return failure(result?.code || 'RECORDS_INCOMPLETE');
        if (result.base_token !== input.base.token || result.user_id !== input.user_id || result.table_id !== table.id) {
          return failure('RESOURCE_MISMATCH');
        }
        tables[table.id] = result.data;
      }
      const comments = await page.evaluate(readComments, { base_token: input.base.token, user_id: input.user_id });
      if (comments?.status !== 'complete') return { ...failure(comments?.code || 'COMMENTS_INCOMPLETE'),
        diagnostic: { tables_read: Object.keys(tables).length,
          records_read: Object.values(tables).reduce((sum, table) => sum + table.rows.length, 0),
          ...(comments?.diagnostic || {}) } };
      return { status: 'complete', format_version: 1, base_token: input.base.token,
        user_id: input.user_id, tables, comments: comments.data };
    };
    const before = await readPass(0);
    if (before.status !== 'complete') return before;
    // A row can move between projects after its table was read. Repeat the
    // complete read in fresh documents before using its links for comments.
    const after = await readPass(1);
    if (after.status !== 'complete') return after;
    if (encode(before) !== encode(after)) return failure('SNAPSHOT_CHANGED');
    return { ...after, consistency: 'two_complete_passes_equal' };
  });
}

async function main() {
  let raw = '';
  process.stdin.setEncoding('utf8');
  for await (const chunk of process.stdin) {
    raw += chunk;
    if (Buffer.byteLength(raw) > 256 * 1024) return failure('INVALID_INPUT');
  }
  let input;
  try { input = JSON.parse(raw); } catch { return failure('INVALID_INPUT'); }
  return collect(input);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main().then((result) => {
  process.stdout.write(JSON.stringify(result) + '\n', () => process.exit(result.status === 'complete' ? 0 : 1));
}).catch((cause) => {
  const code = cause instanceof BrowserCdpError ? cause.code : 'BROWSER_READ_FAILED';
  process.stdout.write(JSON.stringify(failure(code)) + '\n', () => process.exit(1));
});
