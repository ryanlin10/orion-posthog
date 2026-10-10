import { build } from 'esbuild';
await build({ entryPoints: ['src/auto.js'], bundle: true, minify: true, format: 'iife', globalName: 'PostHogBehavioral', outfile: 'dist/recorder.js', target: ['es2020'], sourcemap: true });
