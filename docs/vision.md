# Mandarin Listening Practice App: Vision and MVP Plan

A personal project. This doc captures my goals, the design we worked out, and the open questions, so a new coding assistant can pick it up.

## About me

- Native English speaker, learning Mandarin Chinese.
- Studied for a few years as a kid. Got back into it with an online class starting early last year, took a break at the start of this year, and resumed around August.
- Current study habit: I make **physical flashcards** for new words. That works for learning vocabulary, but I have no system for **listening practice**.

## Goal

Get comfortable **recognizing words I already know when a native-sounding speaker says them** in full sentences.

The app is **not** for learning new words. Flashcards remain how I take in new vocabulary. The listening app should only use words I've already learned.

## Core experience

An iOS app (on my phone) that works like a flashcard deck of audio:

1. I tap and it plays a Chinese sentence.
2. I **think in my head** what the English translation is. I don't speak or type it.
3. I tap again to reveal the answer (hanzi, pinyin, English).
4. I mark it **✓** (got it) or **✗** (wrong or didn't understand).

Daily routine: open the app, go through **~12 sentences prepared for today**, mark each one, done.

### Requirements

- **AI generates the sentences, not me.** It should prepare them **ahead of time** (e.g. a nightly job), so they're ready when I open the app.
- **Only words I know** (plus the basic grammar/function words every sentence needs).
- The app already **knows the English translation**. No answer checking or speech recognition.
- **Native-sounding text-to-speech**, ideally **several voices** (male, female, different ages) so it feels like different real people.
- For the MVP, "12 random sentences using only my known words" is enough.

### History screen

- A long list of every sentence I've done, grouped by day, with my ✓/✗ for each.
- A toggle between Mandarin text and English. **Default: Mandarin.**

## Known-words database

The app needs a database of the words I know. My current sources:

1. **A Discord channel on a server I share with myself.** Each message is usually one word or phrase in Chinese that I want to learn. I write "done" next to a message once I've made a flashcard for it. **Assume I know everything marked done.**
2. **Google Docs from my class teacher.** These work as a "virtual textbook" covering each quarter's new material.

**Later (not MVP):** an SOP/agent workflow I can trigger to sync new words from these sources into the database. Likely implementation: a small Discord bot I add to my own server to read channel history, plus Google Docs access (a link-shared export or the Drive API). For the MVP, a one-time manual import is fine.

## Ideas parked for later

- Saying the English translation aloud and having the app judge whether it's correct. (Deliberately left out of the MVP.)
