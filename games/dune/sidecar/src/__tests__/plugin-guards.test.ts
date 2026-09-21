import { describe, expect, it } from 'vitest';
import { isDevEntityName, isPlayerCharacterClass, mapPluginEvent } from '../dune/mapping.js';
import type { TakaroPlayer } from '../dune/types.js';

const FLS = 'A1B2C3D4E5F60718';
const resolvePlayer = (source: Record<string, unknown>): TakaroPlayer | null =>
  source.ref === 'acct:1' ? { gameId: FLS, name: 'TakaroTest', steamId: '76561198765432109', platformId: 'steam:76561198765432109' } : null;

/**
 * Measured on the live rig 2026-09-21. The plugin classified the player's OWN environment deaths (seqs 4 and 5,
 * instigator == victim) as `entity-killed`, with the victim unresolved and only the UE class to go on — so Takaro was
 * told TakaroTest that it had killed a `BP_DunePlayerCharacter_C`.
 *
 * L2c fixes the classification in the plugin. These are the sidecar-side guards, so that a plugin build WITHOUT that
 * fix still cannot poison Takaro's kill feed or put a dev name in front of a player.
 */
describe('guard 1: a dev/class name never reaches Takaro as a creature', () => {
  const kill = (data: Record<string, unknown>): { type: string; data: unknown } => ({
    type: 'entity-killed',
    data: { player: { ref: 'acct:1' }, ...data },
  });

  it('an entity-killed the catalogue cannot name is dropped, and the drop is counted', () => {
    const drops: string[] = [];
    const mapped = mapPluginEvent(kill({ entity: null, entityCode: 'DuneCritterBase', nameIsClassName: true }), {
      resolvePlayer,
      onDrop: (r) => drops.push(r),
    });
    expect(mapped).toBeNull();
    expect(drops).toEqual(['entityKilledUnnamed']);
  });

  it('nameIsClassName:true means the declared name is NOT trusted, even when one is present', () => {
    const mapped = mapPluginEvent(kill({ entity: 'DuneCritterBase', entityCode: 'DuneCritterBase', nameIsClassName: true }), { resolvePlayer });
    expect(mapped).toBeNull();
  });

  it('a catalogue name lets it through, with exactly the three DTO fields', () => {
    const mapped = mapPluginEvent(kill({ entity: null, entityCode: 'DuneCritterBase', nameIsClassName: true, weapon: 'Crysknife' }), {
      resolvePlayer,
      entityName: (code) => (code === 'DuneCritterBase' ? 'Desert Mouse' : undefined),
    });
    expect(mapped?.type).toBe('entity-killed');
    expect(mapped?.data).toMatchObject({ entity: 'Desert Mouse', weapon: 'Crysknife' });
    expect(Object.keys(mapped?.data ?? {}).sort()).toEqual(['entity', 'player', 'weapon']);
    // The full IGamePlayer, not just gameId+name.
    expect(mapped?.data.player).toMatchObject({ gameId: FLS, steamId: '76561198765432109', platformId: 'steam:76561198765432109' });
  });

  it('even a catalogue that answers with a dev name is refused — the rule is about what Takaro SHOWS', () => {
    const mapped = mapPluginEvent(kill({ entityCode: 'SomeCritter' }), { resolvePlayer, entityName: () => 'BP_Critter_C' });
    expect(mapped).toBeNull();
  });

  it('isDevEntityName knows an asset id from a creature name', () => {
    for (const dev of ['BP_DunePlayerCharacter_C', 'DuneCritterBase', 'T3_Band_Slv_Reg_Marksman', 'SK_Worm', 'Thing_C', '', null]) {
      expect(isDevEntityName(dev)).toBe(true);
    }
    for (const real of ['Sandworm', 'Desert Mouse', 'Scavenger Thug', 'Shai-Hulud']) {
      expect(isDevEntityName(real)).toBe(false);
    }
  });
});

describe('guard 2: a player character is never an entity kill', () => {
  it('the exact live payload becomes ONE player-death for that player, not a creature kill', () => {
    const drops: string[] = [];
    const mapped = mapPluginEvent(
      { type: 'entity-killed', data: { player: { ref: 'acct:1' }, entity: null, nameIsClassName: true, entityClass: 'BP_DunePlayerCharacter_C' } },
      { resolvePlayer, onDrop: (r) => drops.push(r) },
    );
    expect(mapped?.type).toBe('player-death');
    expect(mapped?.data.player).toMatchObject({ gameId: FLS, name: 'TakaroTest' });
    // Nobody killed him: it is his own death, so there is no attacker to name.
    expect(mapped?.data.attacker).toBeUndefined();
    expect(Object.keys(mapped?.data ?? {}).sort()).toEqual(['msg', 'player']);
    expect(drops).toEqual(['entityKilledPlayerCharacter']);
  });

  it('it wins over the naming guard, so a named player class is still not a kill', () => {
    const mapped = mapPluginEvent(
      { type: 'entity-killed', data: { player: { ref: 'acct:1' }, entityCode: 'DunePlayerCharacter' } },
      { resolvePlayer, entityName: () => 'Fremen Warrior' },
    );
    expect(mapped?.type).toBe('player-death');
  });

  it('a real creature is untouched by it', () => {
    const mapped = mapPluginEvent({ type: 'entity-killed', data: { player: { ref: 'acct:1' }, entityCode: 'ASandwormPawn' } }, {
      resolvePlayer,
      entityName: () => 'Sandworm',
    });
    expect(mapped?.type).toBe('entity-killed');
    expect(mapped?.data.entity).toBe('Sandworm');
  });

  it('isPlayerCharacterClass matches the class under every key the plugin uses', () => {
    for (const cls of ['BP_DunePlayerCharacter_C', 'DunePlayerCharacter', 'ADunePlayerPawn', 'playercharacter']) {
      expect(isPlayerCharacterClass(cls)).toBe(true);
    }
    for (const cls of ['ASandwormPawn', 'DuneNpcCharacter', 'DuneCritterBase', '', null]) {
      expect(isPlayerCharacterClass(cls)).toBe(false);
    }
  });
});

describe('guard 3: nobody is their own killer', () => {
  it('instigator == victim by ref means no attacker, and the message says "died"', () => {
    const mapped = mapPluginEvent(
      { type: 'player-death', data: { player: { ref: 'acct:1' }, killer: { ref: 'acct:1' }, cause: 'Falling' } },
      { resolvePlayer },
    );
    expect(mapped?.type).toBe('player-death');
    expect(mapped?.data.attacker).toBeUndefined();
    expect(String(mapped?.data.msg)).not.toMatch(/killed by TakaroTest/);
  });

  it('instigator == victim by gameId is caught too, not only by ref', () => {
    const mapped = mapPluginEvent(
      { type: 'player-death', data: { player: { flsId: FLS, characterName: 'TakaroTest' }, killer: { flsId: FLS, characterName: 'TakaroTest' } } },
      {},
    );
    expect(mapped?.data.attacker).toBeUndefined();
  });

  it('a REAL PvP kill still gets its attacker — the guard must not eat those', () => {
    const other = 'AAAABBBBCCCCDDDD';
    const mapped = mapPluginEvent(
      { type: 'player-death', data: { player: { ref: 'acct:1' }, killer: { ref: 'acct:2' } } },
      {
        resolvePlayer: (s) =>
          s.ref === 'acct:1'
            ? { gameId: FLS, name: 'TakaroTest' }
            : s.ref === 'acct:2'
              ? { gameId: other, name: 'Rival' }
              : null,
      },
    );
    expect(mapped?.data.attacker).toMatchObject({ gameId: other, name: 'Rival' });
    // With a real `attacker` present Takaro composes the sentence itself, so we add no `msg` of our own —
    // `withDeathMessage` only writes one for the cases Takaro cannot phrase (an NPC, or nobody at all).
    expect(mapped?.data.msg).toBeUndefined();
  });
});
