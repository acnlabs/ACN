import { describe, expect, it } from 'vitest';
import {
  modalityFromProviderProps,
  modalitiesFromProviderBody,
} from '../src/commands/model-heartbeat.js';

describe('modalityFromProviderProps', () => {
  it('reads OpenRouter architecture.modality', () => {
    expect(
      modalityFromProviderProps({
        id: 'x',
        architecture: { modality: 'text+image->text' },
      })
    ).toBe('text+image->text');
  });

  it('maps input_modalities image', () => {
    expect(
      modalityFromProviderProps({ id: 'x', input: ['text', 'image'] })
    ).toBe('text+image->text');
  });

  it('prefixes provider ids from the catalog body', () => {
    const rows = modalitiesFromProviderBody(
      {
        data: [
          { id: 'kimi-k2.5', architecture: { modality: 'text->text' } },
          { id: 'vision', input_modalities: ['text', 'image'] },
        ],
      },
      'tencenttokenplan'
    );
    expect(rows).toEqual([
      { model_id: 'tencenttokenplan/kimi-k2.5', modality: 'text->text' },
      { model_id: 'tencenttokenplan/vision', modality: 'text+image->text' },
    ]);
  });
});
