// Runs an Orion sequence model through onnxruntime-web, as the browser does.
// usage: node onnx_predict.mjs model.json model.onnx cases.json → JSON predictions, one per case (flattened steps)
import { readFileSync } from 'node:fs';
import * as ort from 'onnxruntime-web';
import { predictSteps } from '../../src/model.js';

const spec = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const session = await ort.InferenceSession.create(readFileSync(process.argv[3]));
const out = [];
for (const steps of JSON.parse(readFileSync(process.argv[4], 'utf8'))) out.push(await predictSteps(spec, session, ort, Float32Array.from(steps)));
console.log(JSON.stringify(out));
