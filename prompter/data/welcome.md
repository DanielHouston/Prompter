# Welcome

Hi everyone, and welcome back to the channel. Today we're looking at how to turn an
Elgato Prompter into a proper voice-following teleprompter on Linux. [smile]

<!-- Comments like this one never show up on the prompter. -->

## The problem

Camera Hub only runs on Windows and macOS, so on Linux the prompter is just a second
screen. That's fine for reading notes, but it won't scroll along with you while you talk.

## The fix

We listen to the microphone, transcribe the last few seconds with Whisper on the graphics
card, and line that up against the script. Wherever the words match is where you are.
If you go off script for a moment, nothing moves until you come back to it. [pause]

Headings like the ones above become sections you can jump between, and anything in
square brackets is a cue for you rather than something to say out loud.

## Wrapping up

That's the whole idea. Write your script in Markdown, save it in the Prompter folder,
and start talking. Thanks for watching, and I'll see you in the next one.
