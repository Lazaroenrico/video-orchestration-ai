import { experimental_generateVideo as generateVideo } from 'ai';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => {
      data += chunk;
    });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

function assertGatewayCredentials() {
  if (!process.env.AI_GATEWAY_API_KEY && !process.env.VERCEL_OIDC_TOKEN) {
    throw new Error('AI_GATEWAY_API_KEY or VERCEL_OIDC_TOKEN is required');
  }
}

async function normalizeImage(image) {
  if (!image) {
    return undefined;
  }
  if (image.kind === 'data_uri') {
    return image.uri;
  }
  if (image.kind === 'url') {
    return image.uri;
  }
  if (image.kind === 'path') {
    return await readFile(image.path);
  }
  throw new Error(`Unsupported image kind: ${image.kind}`);
}

async function main() {
  const payload = JSON.parse(await readStdin());
  assertGatewayCredentials();

  const promptImage = await normalizeImage(payload.image);
  const prompt = promptImage
    ? { image: promptImage, text: payload.promptText }
    : payload.promptText;

  const { video } = await generateVideo({
    model: payload.model,
    prompt,
    duration: payload.duration,
    aspectRatio: payload.aspectRatio,
    resolution: payload.resolution,
    generateAudio: payload.generateAudio,
    abortSignal: AbortSignal.timeout(payload.timeoutMs ?? 900000),
  });

  const bytes = video?.uint8Array
    ? Buffer.from(video.uint8Array)
    : Buffer.from(video.base64, 'base64');
  if (!bytes.length) {
    throw new Error('Seedance returned an empty video payload');
  }

  await mkdir(dirname(payload.outputPath), { recursive: true });
  await writeFile(payload.outputPath, bytes);
  process.stdout.write(JSON.stringify({
    ok: true,
    outputPath: payload.outputPath,
    bytes: bytes.length,
  }));
}

main().catch(error => {
  process.stdout.write(JSON.stringify({
    ok: false,
    error: error?.stack || error?.message || String(error),
  }));
  process.exitCode = 1;
});
