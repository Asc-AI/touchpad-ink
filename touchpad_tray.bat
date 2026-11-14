@echo off
setlocal
cd /d "%~dp0"
title Touchpad Handwriting Tray
echo Touchpad handwriting tray is running.
echo   Tray icon    left or right click: auto / typing / full-screen / PPT / Note
echo   Start at login: below the modes the menu has "start at sign-in"; it
echo                writes HKCU Run for this user only (no admin, no task);
echo                a check mark means enabled. Re-run a moved build once to
echo                rewrite the stored path
echo   Auto mode    one check per Alt hold: OneNote -^> Note ink, slide show
echo                -^> PPT ink, text caret -^> caret overlay, otherwise nothing
echo   Hold Alt     write. Typing: capture starts on Alt down, so the first
echo                stroke is never lost; recognition runs WHILE you write
echo                and the bar refreshes about every 0.12 s (no pause
echo                needed). Release re-reads only when the last live
echo                result missed the final points. Full-screen keeps the
echo                annotation; PPT / Note finish the left-button ink
echo   Alt again    clear our ink and start a new one (typing: clears this round)
echo   Double Alt   continuous writing without holding: type nonstop (the
echo                same live refresh about every 0.12 s; digits/clicks
echo                select while the capture keeps running), PPT move and
echo                rebuild
echo                the region, double Alt again to exit; in
echo                continuous the RIGHT button is NOT swallowed: it reaches
echo                the app (context menu) and also exits continuous
echo   PPT / Note   pick the app's pen first (slideshow pen, OneNote draw tab);
echo                ink belongs to that app and survives after quitting
echo   PPT region   local mapping at the pointer on each Alt press: default 3
echo                (about a third of a slide) .. 6, whole slide = 1; the
echo                pointer sits upper-left inside it, room to write right;
echo                the pad is a mouse, not a tablet: a pen-down starts at
echo                the last tip plus the hand's lift-to-plant travel, so
echo                strokes neither chain end-to-end nor jump to the pad's
echo                absolute spot; a long, level leftward re-plant counts as
echo                the wrist returning (the pen stays), and one lift can
echo                nudge the pen at most LIFT_MAX_TRAVEL; the box stays put
echo                until little room is left right/down, then shifts
echo                forward (old box outlined, compound frame); the build
echo                point is frozen when Alt goes down, so the pad dragging
echo                the mouse during the hold gate cannot move it; near the
echo                slide edge it clamps; a zoom mid-stroke never jumps the
echo                pen
echo                pinch with BOTH fingers moving (or the wheel) while
echo                holding Alt zooms only OUR region, never the slide,
echo                even while Alt is moving the pointer (the pad's
echo                Ctrl+wheel pinch is always swallowed); a resting
echo                palm never zooms or inks; the pale blue frame flashes
echo                on Alt down and while zooming. The scale follows every
echo                pad report, but the frame repaints at about 33 fps so a
echo                fast pinch stays smooth (the wheel repaints at once)
echo   Pad edges    a contact that first lands on the outer border (110 left/
echo                right, 60 top/bottom pad px) never starts a stroke: that
echo                is usually the wrist, and dragging it used to sweep a
echo                long unwanted line; start just inside to write there
echo   Status       PPT: inside the pale blue frame the moment it appears
echo                (preview / build / wheel or pinch zoom, with live size);
echo                typing: the bar shows ONLY the candidates; the one
echo                exception is idle continuous writing (no candidates, no
echo                ink): a short "continuous" status sits on the left and
echo                clears as soon as a stroke starts; a real error notice is
echo                the only other text; Note / full-screen: small card
echo   Corners      every frame (region, status chip, card, typing bar) uses
echo                the Win11 rounded look, scaled with the DPI; the typing
echo                bar and the selected candidate use an 8 logical px arc
echo                (Tk arcs are not anti-aliased, so a bigger radius looks
echo                jagged); the bar has no square highlight border (it sat
echo                outside the children and showed a black corner), its
echo                straight edges are 1 px strips under the corner masks
echo   One instance only: if another tray is running, press Ctrl+F8 in the
echo                old one first, then start this build again
echo   Esc          cancel / clear
echo   Ctrl+F8      quit the whole tray (Ctrl+Shift+F8 if Ctrl+F8 is taken)
echo Keep this window open (it can stay behind other apps).
rem set TOUCHPAD_INK_REGION=3
python -u tools\tray_app.py
if errorlevel 1 (
  echo.
  echo Tray startup failed. No mouse capture is active.
  pause
)
endlocal
