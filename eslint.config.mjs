import js from '@eslint/js';
import { defineConfig, globalIgnores } from 'eslint/config';
import tseslint from 'typescript-eslint';

export default defineConfig([
  globalIgnores(['npu_top/static/**', 'dist/**', 'node_modules/**', '.next/**', '.wrangler/**']),
  js.configs.recommended,
  ...tseslint.configs.recommended,
]);
