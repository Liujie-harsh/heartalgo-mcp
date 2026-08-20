import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'


const root = new URL('../', import.meta.url)
const manifest = JSON.parse(await readFile(new URL('package.json', root), 'utf8'))
const patch = await readFile(new URL('cordis.patch.yml', root), 'utf8')

assert.equal(manifest.name, 'heart-algo-dsh-plugin')
assert.equal(manifest.dsh?.bundle?.patch, './cordis.patch.yml')
assert.ok(manifest.files.includes('cordis.patch.yml'))

for (const fragment of [
  '- insert:',
  'id: heart-algo-mcp',
  "name: '@deepseek-ai/dsh-mcp-client'",
  'serverName: heart-algo',
  'transport: streamable-http',
  'HEART_ALGO_MCP_URL',
  'HEART_ALGO_MCP_TOKEN',
  'toolCallTimeoutMs: 60000',
]) {
  assert.ok(patch.includes(fragment), `cordis.patch.yml missing: ${fragment}`)
}

assert.ok(!/Authorization:\s+Bearer\s+(?![`$])/i.test(patch), 'embedded token detected')

console.log('heart-algo-dsh-plugin: OK')
