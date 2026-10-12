#!/usr/bin/env node
// `npx github:ryanlin10/orion-posthog` runs this: the guided setup in setup/main.js.
const [major] = process.versions.node.split('.').map(Number);
if (major < 20) {
  console.error(`Orion setup needs Node.js 20 or newer; this is ${process.version}.`);
  process.exit(1);
}
const { main } = await import('../setup/main.js');
process.exitCode = await main(process.argv.slice(2));
