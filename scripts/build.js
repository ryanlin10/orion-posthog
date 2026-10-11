import { build } from 'esbuild';
await build({ entryPoints: ['src/auto.js'], bundle: true, minify: true, format: 'iife', globalName: 'Orion', outfile: 'dist/orion.js', target: ['es2020'], sourcemap: true });
