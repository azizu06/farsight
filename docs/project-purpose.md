# Farsight: an adaptable remote robot

Farsight is a general-purpose remote robot: an operator can see, explore,
understand and communicate with a place from elsewhere. Live camera viewing,
room mapping, person and enrolled-face recognition, manual and autonomous
navigation, AI scene questions, and two-way voice are capabilities of the same
platform. A particular application selects the capabilities it needs.

The general pitch is: **see a place remotely, move through it, understand what
is there, and speak with the people there.** The Jetson supplies perception and
local robot control. The phone supplies camera, depth and pose, plus
microphone/speaker endpoints. The Mac handles retained room rendering and the
Quest provides the operator's view and controls. The existing transport and
compute split support this product direction.

## Example: information, alerts and evacuation announcements

An operator positions or drives the robot where information is needed, looks
around through the remote camera, speaks through the rover phone's speaker and
hears replies through the intercom. The same robot could provide information
at an event, announce a change, draw attention with an alarm, or relay spoken
evacuation instructions. This is one application of the adaptable platform.

Two-way speech has a toggle so broadcasting is deliberate. Alarms and preset
alert playback are possible extensions; describe them as working features
once implemented and tested. An operator can use the intercom to communicate
evacuation instructions and listen to the people receiving them.

## Other applications and demo framing

Remote exploration, inspection, finding an enrolled person, communicating with
someone in another room, and reviewing a mapped space are other examples. A
prize-specific conversation can focus on an appropriate example while Devpost
presents the adaptable platform.

Keep software implementation, deployed features and observed hardware behavior
distinct. Two-way intercom, recognition, mapping and navigation retain their
existing physical acceptance requirements.
