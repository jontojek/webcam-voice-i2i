# 🎙️ Voice Prompt Guide — FLUX2-Klein-4B Edit Model

This model is a **2-step img2img editor** — it takes your webcam frame and transforms it
based on what you say. It responds best to **short, punchy phrases** rather than long
sentences. You're not writing a novel; you're shouting an art direction at a fast model.

A few things that work well:
- **"I am..."** anchors the edit to your appearance
- **"Everything is..."** shifts the whole scene
- **Single material words** (`glass`, `gold`, `ice`) produce very clean texture transforms
- **Lighting words** (`neon`, `candlelight`, `infrared`) shift color grading dramatically
- **Combos** of scene + material + lighting give the most visually striking results
- **Shorter = faster to transcribe** — Whisper picks up 2–4 word phrases very reliably

Speak clearly, let the phrase land, then try the next one.

---

## Scene / Environment

```
the background is on fire
everything is underwater
deep in outer space
inside a thunderstorm
inside a tornado
dense fog everywhere
submerged in lava
snowing heavily
inside a haunted forest
floating in clouds
in a burning city
inside a cave of crystals
in a coral reef
in a desert at noon
inside a blizzard
inside a neon-lit city at night
in a post-apocalyptic wasteland
inside a volcano
inside a mirror maze
in a dark cathedral
inside a submarine
deep in a jungle
on the surface of the moon
inside a clock tower
in an ancient ruin
```

---

## Appearance / Material Transforms

```
I am a skeleton
I am made of glass
I am made of gold
I am made of ice
my skin is made of bark
I am covered in moss
I am made of marble
I am covered in circuits
I am made of clay
I am made of sand
I am melting
I am crystallizing
I am made of smoke
I am made of obsidian
I am covered in large spiders
I am covered in vines
I am wrapped in chains
I am made of fire
I am dissolving into pixels
I am a robot
I am covered in feathers
I am made of wax
I am made of stone
I am made of coral
I am rusting
I am covered in moss and lichen
I am made of stained glass
I am translucent
```

---

## Lighting and Atmosphere

```
lights are off
only candlelight
blinding white light
bathed in red emergency light
lit by neon signs
backlit silhouette
strobing lights
deep purple ambient light
golden hour sunlight
moonlight only
infrared light
UV blacklight
laser grid lighting
flickering fluorescent light
lightning strike illumination
bioluminescent glow
dim green laboratory light
burning torchlight
```

---

## Art Style

```
oil painting style
watercolor painting
Van Gogh style
charcoal sketch
anime style
comic book style
photorealistic render
film noir black and white
vintage photograph
glitch art
stained glass style
pixel art
renaissance painting
impressionist painting
cubist painting
ink wash painting
risograph print
woodblock print
expressionist painting
Art Nouveau style
```

---

## Creature / Character

```
I am a werewolf
I am a vampire
I am a ghost
I am a demon
I am a zombie
I am a cyborg
I am a stone statue
I am a mummy
I am an alien
I am a shadow creature
I have wings
my eyes are glowing
I am a giant
I am very small
I am an ancient warrior
I am a deep sea creature
I am a plague doctor
I am a medieval knight
I am a witch
I am a sea monster
I am a forest spirit
```

---

## Mood and Emotional Atmosphere

```
everything is surreal
dreamlike and soft
dark and ominous
joyful and vibrant
chaotic energy
calm and meditative
tense and claustrophobic
euphoric and glowing
melancholy and grey
cosmic horror
whimsical and strange
serene and timeless
violent and explosive
peaceful and golden
```

---

## Power Combos

These multi-part phrases combine scene, material, lighting, and style for maximum impact.
Say them slowly and clearly — Whisper handles them well as a single utterance.

```
I am a skeleton on fire underwater
I am made of glass inside a thunderstorm
robot in a burning forest at night
ice sculpture inside a volcano
I am a ghost in a neon city
zombie in a coral reef
I am made of gold in outer space
crystal cave with candlelight
I am a shadow creature in a blizzard
oil painting of a demon at golden hour
charcoal sketch in a haunted forest
vampire in a neon-lit city at night
I am made of stained glass in a cathedral
cyborg dissolving into pixels
werewolf in a snowstorm at moonlight
ancient warrior covered in gold
I am made of smoke in a burning city
alien deep underwater in bioluminescent light
ghost made of ice in a dark cathedral
anime style cyberpunk neon rain
```

---

## Quick Reference (printable)

| Category | Examples |
|---|---|
| Scene | `on fire` · `underwater` · `outer space` · `blizzard` · `neon city` |
| Material | `skeleton` · `glass` · `gold` · `ice` · `obsidian` · `smoke` |
| Lighting | `candlelight` · `neon` · `moonlight` · `infrared` · `blacklight` |
| Style | `oil painting` · `charcoal sketch` · `anime` · `film noir` · `glitch art` |
| Character | `skeleton` · `ghost` · `cyborg` · `vampire` · `demon` · `robot` |
| Mood | `surreal` · `ominous` · `euphoric` · `dreamlike` · `chaotic` |
