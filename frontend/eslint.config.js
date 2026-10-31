import js from '@eslint/js';
import ts from 'typescript-eslint';
import vue from 'eslint-plugin-vue';
import globals from 'globals';
import prettier from 'eslint-config-prettier';

export default ts.config(
  { ignores: ['dist/**', 'node_modules/**'] },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...vue.configs['flat/recommended'],
  {
    files: ['**/*.vue', '**/*.ts'],
    languageOptions: {
      parserOptions: { parser: ts.parser },
      globals: { ...globals.browser, ...globals.node },
    },
    rules: { 'vue/multi-word-component-names': 'off', 'vue/max-attributes-per-line': 'off' },
  },
  prettier,
);
