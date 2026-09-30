"""Autoplay groundwork: Jarvis plays a game while Shawn watches (e.g. the Skyrim intro he has
seen a thousand times).

Pipeline, one tick at a time:
  observe.py   screenshot of the game window (+ OCR on demand)
  policy.py    decides what to do next: a scripted routine, or a vision model
  session.py   runs the loop, executes each step, logs everything for replay
  inputs.py    sends keyboard/mouse input (or, in dry run, only records it)
  guard.py     operator controls: kill switch, stop the moment Shawn touches the
               keyboard/mouse, act only while the game window has focus

Which games to use it on is Shawn's call - there are no per-game rules in here.
"""
