import assert from "node:assert/strict";
import { test } from "node:test";
import { parseSilences, planChunks } from "../audio.js";
import {
  assignSpeakers,
  cleanWhisper,
  isHallucination,
  normalizeSpeakers,
  SpeakerTimeline,
  toTxt,
  wordsToSegments,
  type Word,
} from "../merge.js";

const w = (text: string, start: number, end: number, speaker?: string): Word => ({ text, start, end, speaker });

test("wordsToSegments: nytt stycke vid talarbyte och lång paus", () => {
  const segs = wordsToSegments([
    w("Hej", 0, 0.4, "0"),
    w("där.", 0.5, 0.9, "0"),
    w("Tjena!", 1.2, 1.6, "1"),
    w("Efter", 5, 5.3, "1"),
    w("paus.", 5.4, 5.8, "1"),
  ]);
  assert.deepEqual(
    segs.map((s) => [s.speaker, s.text]),
    [
      ["0", "Hej där."],
      ["1", "Tjena!"],
      ["1", "Efter paus."],
    ],
  );
});

test("assignSpeakers delar ett Whisper-stycke vid talarbytet", () => {
  const timeline = new SpeakerTimeline([
    w("hej", 0, 0.5, "0"),
    w("hur", 0.6, 0.9, "0"),
    w("mår", 1.0, 1.3, "0"),
    w("du", 1.4, 1.6, "0"),
    w("bra", 2.0, 2.3, "1"),
    w("tack", 2.4, 2.8, "1"),
  ]);
  const segs = assignSpeakers(
    [{ start: 0, end: 2.9, text: " Hej, hur mår du? Bra, tack." }],
    [w("Hej", 0, 0.5), w("hur", 0.6, 0.9), w("mår", 1.0, 1.3), w("du", 1.4, 1.6), w("Bra", 2.0, 2.3), w("tack", 2.4, 2.8)],
    timeline,
  );
  assert.deepEqual(
    segs.map((s) => [s.speaker, s.text]),
    [
      ["0", "Hej, hur mår du?"],
      ["1", "Bra, tack."],
    ],
  );
});

test("assignSpeakers använder dominerande talare när orden inte går att para ihop", () => {
  const timeline = new SpeakerTimeline([w("a", 0, 1, "1"), w("b", 1, 3, "1"), w("c", 3, 3.2, "0")]);
  const segs = assignSpeakers([{ start: 0, end: 3.2, text: "Två tusen kronor" }], [w("2000", 0, 1), w("kronor", 1, 3)], timeline);
  assert.equal(segs.length, 1);
  assert.equal(segs[0]!.speaker, "1");
});

test("ett enstaka avvikande ord byter inte talare", () => {
  const timeline = new SpeakerTimeline([w("a", 0, 0.4, "0"), w("b", 0.5, 0.9, "1"), w("c", 1.0, 1.4, "0")]);
  const segs = assignSpeakers([{ start: 0, end: 1.5, text: "ett två tre" }], [w("ett", 0, 0.4), w("två", 0.5, 0.9), w("tre", 1.0, 1.4)], timeline);
  assert.deepEqual(segs.map((s) => s.text), ["ett två tre"]);
});

test("kända Whisper-påhitt filtreras bort", () => {
  assert.ok(isHallucination({ start: 0, end: 2, text: " Textning.nu" }));
  assert.ok(isHallucination({ start: 0, end: 2, text: "Undertexter från Amara.org-gemenskapen" }));
  assert.ok(isHallucination({ start: 0, end: 2, text: "Tack för att ni tittade!" }));
  assert.ok(!isHallucination({ start: 0, end: 2, text: "Tack för att ni kom hit i dag." }));
  assert.ok(isHallucination({ start: 0, end: 2, text: "bla bla", compression_ratio: 3.1 }));
});

test("cleanWhisper tar bort kort text där Deepgram inte hörde något tal", () => {
  const timeline = new SpeakerTimeline([w("hej", 0, 0.5, "0")]);
  const kept = cleanWhisper(
    [
      { start: 0, end: 0.6, text: "Hej." },
      { start: 30, end: 32, text: "Tack så mycket." },
    ],
    timeline,
  );
  assert.deepEqual(kept.map((s) => s.text), ["Hej."]);
});

test("normalizeSpeakers numrerar i ordning", () => {
  const segs = normalizeSpeakers([
    { speaker: "3", start: 0, end: 1, text: "a" },
    { speaker: "1", start: 1, end: 2, text: "b" },
    { speaker: "3", start: 2, end: 3, text: "c" },
  ]);
  assert.deepEqual(segs.map((s) => s.speaker), ["0", "1", "0"]);
});

test("planChunks delar i tystnad och täcker hela ljudet", () => {
  const silences = [
    { start: 100, end: 101 },
    { start: 500, end: 502 },
    { start: 1100, end: 1101 },
  ];
  const chunks = planChunks(1500, silences, 600);
  assert.equal(chunks[0]!.start, 0);
  assert.equal(chunks[0]!.end, 501);
  assert.equal(chunks[chunks.length - 1]!.end, 1500);
  for (let i = 1; i < chunks.length; i++) assert.equal(chunks[i]!.start, chunks[i - 1]!.end);
  for (const c of chunks) assert.ok(c.end - c.start <= 600);
});

test("planChunks utan tystnad delar vid maxlängden", () => {
  const chunks = planChunks(1300, [], 600);
  assert.deepEqual(chunks, [
    { start: 0, end: 600 },
    { start: 600, end: 1200 },
    { start: 1200, end: 1300 },
  ]);
});

test("parseSilences läser ffmpegs utskrift", () => {
  const log = "[silencedetect @ 0x1] silence_start: 1.5\n[silencedetect @ 0x1] silence_end: 2.75 | silence_duration: 1.25\n";
  assert.deepEqual(parseSilences(log), [{ start: 1.5, end: 2.75 }]);
});

test("toTxt med talarnamn och tider", () => {
  const txt = toTxt(
    { title: "Möte", createdAt: "2026-11-17T09:00:00Z", durationSec: 65, draft: false },
    [
      { speaker: "0", start: 0, end: 3, text: "Hej." },
      { speaker: "1", start: 61, end: 64, text: "Hejsan." },
    ],
    { "0": "Anna" },
    { timestamps: true, speakers: true },
  );
  assert.match(txt, /^Möte\n====/);
  assert.match(txt, /Talare: Anna, Talare 2/);
  assert.match(txt, /\[00:00\] Anna:\nHej\./);
  assert.match(txt, /\[01:01\] Talare 2:\nHejsan\./);
});
