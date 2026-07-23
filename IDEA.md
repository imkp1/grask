# grask

*An idea, now an alpha. Sharing it because I still want to know if it's stupid.*

---

## The thing I noticed

I read about something. I nod. It makes sense. I close the tab.

Two weeks later someone asks me about it and what comes out of my mouth is a shape — the
right words in roughly the right order, with nothing behind them. I knew *of* the thing. I
didn't know it.

From the inside, those two states are identical. That's the whole problem. There is no
internal signal that distinguishes "I understand this" from "I have watched this be
explained." They feel exactly the same, which means you cannot catch it by being honest
with yourself, or by being smart, or by trying harder. The feeling of understanding is not
evidence of understanding.

This has always been true. Something recent made it much worse.

## What agents did to this

I write a lot of code with Claude now. Most of us do.

Here's what that looks like: I describe a problem. An agent produces something good. I read
it, it makes sense, I ship it. Repeat that across a few PRs and something strange happens —
I now have a *pattern in production* that I could not defend in an interview. I've watched
it work three times. I've never once had to know why.

That used to be impossible. Getting code to work *was* the forcing function; you couldn't
ship what you didn't understand, because it wouldn't run. That forcing function is gone.
Shipping and understanding have come apart, and nothing has replaced the gap.

So you get people — I am one — accumulating enormous surface area and much less depth than
their commit history implies. Not because anyone's lazy. Because the thing that used to
force the issue quietly stopped.

## The idea

**One question about the code you just shipped.**

You close a Claude session. It reads what happened and, most days, decides there is nothing
worth asking — if you spent the session renaming variables and fixing CI, there is nothing
to be hollow about, and asking anyway would just be nagging. When there *is* something, one
question is waiting the next time you type `/grask`, about something you actually engaged
with — usually whatever you stopped to ask about. Twenty seconds. Pick an option, or press
enter and it's gone.

```
from 2026-07-21 · retry wrapper around the payment call

Your wrapper retries the charge on timeout. What makes the retry safe when the
first attempt succeeded but the response was lost on the way back?

  a) Stripe deduplicates identical charge amounts within a short window.
  b) An idempotency key that stays the same across every retry of one charge.
  c) The timeout means the request never reached Stripe, so there is nothing to dedupe.
  d) Reading back the charge list before retrying, and bailing if one matches.

pick   [a-d]   ·   enter = skip   ·   /wrong
> c
✗ A timeout tells you nothing about whether the server processed the request —
only that no response came back in time. Safety comes from a key Stripe can match
the retry against, not from what the timeout implies.
```

Note what that question isn't. It isn't "what is a retry policy." You can answer that one
from memory, and answering it proves nothing. It's about the code *I shipped this
afternoon*, and the only way to be sure which option is right is to actually understand what
I shipped.

That's the whole product. Not a course. Not a tutorial. Not another AI explaining
idempotency to you — you can get that anywhere in fifteen seconds and it's worth about
that much.

The scarce thing isn't explanation. It's **calibration** — the gap between how well you
think you understand something and how well you actually do. A wrong answer you knew was a
guess is fine. A wrong answer you'd have bet on is the whole point: you, catching the thing
you're most confidently wrong about before it hardens into production knowledge, and then
knowing which fifteen seconds of explanation are *yours*.

I tried to measure that certainty directly — a *"how sure are you?"* tap before you
answered. It's cut. Its payoff was never in the moment anyway; it needed the second visit
below to mean anything, and until that exists it was asking for a second keypress against a
twenty-second promise.

Then there's the half that matters most, and I want to be plain that it isn't built yet. A
week later the tool should come back — not a footnote, because you *do* forget and you don't
notice that either. Not the same question: whether last Tuesday's hollow spot filled in.
That second visit is where a one-off quiz becomes evidence that understanding actually
formed. Right now grask catches the gap and never checks back, which makes it half a
product.

## Why it might work

**It can't be fooled by fluency, and I can.** Left alone, I measure my own understanding by
how smoothly the words come out. That metric is worthless — fluency is the *symptom* of the
disease. There is nowhere in a four-option pick to be articulate. Either you recognise the
mechanism or you don't, and the three wrong options are written by something that knows what
half-understanding this particular decision would look like.

**It only needs one honest failure to pay for itself.** Getting told "you've used this
pattern three times and can't explain why it works" once is worth more than a month of
courses, because now you know where to actually look.

**The half a human would fail at needs nothing remembered.** Noticing, on a Tuesday, that
something in this session was worth revisiting is where every tool built on good intentions
goes to die. That half runs on its own, when a session ends, on a topic pulled out of the
session itself. I never have to decide anything was interesting, or feed it, or open a
thing.

That is a weaker claim than the one I started with, and it's worth saying so. I wrote that
the question fires as the session closes and there is nothing to open. It doesn't, and there
is: a question written into a terminal you have already walked away from is a question
nobody reads, so capture pushes and delivery pulls. What's left of the ask is six
characters. The step where a human volunteers input is still gone; the step where they type
`/grask` is not.

That is the leftover of a bigger change of mind. The first version of this was a little web
app I'd visit when I felt like it. I wrote "the user will not show up daily" into my own
design doc as a constraint, designed carefully around it, and somehow didn't notice I'd just
described a product with no users. The portal is gone. What survives of "you have to show
up" is one command inside the editor you were already in, which is about as small as I know
how to make it.

## Why it might not

**The questions might just be bad, and that's the whole ballgame.** A flat explanation
afterwards is annoying; you shrug it off. A mediocre *question* makes the entire interaction
feel fake, and fake is unrecoverable — "what is a retry policy" tells you instantly this
thing has nothing to teach you, and you never open it again. So the bar isn't "a relevant
question." It's a question that makes an experienced engineer stop for thirty seconds and go
*"…huh"* — from their own code, reliably. I don't know yet that an LLM can clear that bar.
If it can't, none of the rest of this matters, and it is still the thing I most need to
measure and haven't.

**Recognising the right answer is easier than knowing it, and I don't know how much
easier.** The first version asked you to explain yourself in your own words and had a model
grade the explanation. That grader had two ways to fail and they were opposite. Ask an LLM
to assess your reasoning and it will tell you you're doing great — accept a vague answer,
quietly fill in what you left out, congratulate you. LLMs are cowards, and a grask that
doesn't grask is an expensive machine for making me feel smart. The worse one, which I
underrated: the grader misreads the code, invents a bug I didn't write, and tells me
confidently I don't understand something I do. Flattery wastes my time. A confident false
accusation about *my own code, in front of me* gets the plugin disabled forever — and it
should, because trust is the whole product and it's decided in the first ten interactions.

So there is no grader. Four options, one right, the answer written at the same moment as the
question, the grade decided by comparing two numbers. Nothing in the loop can flatter me and
nothing can accuse me. The design leans the whole way to one side — it *asks*, it never
*accuses* — and "your premise is wrong, that's not what my code does" is a first-class
answer that ends the question with no penalty and gets logged as a bug against grask, not
against me.

What I gave up is the part that couldn't be bluffed. I could not have talked my way past a
blank box. I might well eliminate three options and collect a checkmark having understood
nothing, and I won't know that happened. Everything now rests on the wrong options: each one
has to be a mechanism you would actually believe if you half-understood the decision, which
is a much harder thing to generate than a plausible-looking sentence. If that turns out not
to be enough, this reopens.

And the confident-wrong risk didn't disappear, it moved earlier. The answer key is written
by a model reading my code, so when it misreads, it marks a right answer wrong with a
confident explanation and there is no grader left to blame for it. Smaller blast radius than
a paragraph about why I don't understand my own work — but the same failure, and still the
thing I most need to get right after the questions themselves.

**Maybe nobody cares.** Claude wrote it, tests passed, PR merged, salary arrived — why
spend another twenty seconds to find out you're hollow? I used to answer "because
eventually someone asks you in an interview," and that's not enough: a cost today against a
benefit six months out, maybe. I could claim it prevents outages or catches rot before it
ships. I don't know that, and dressing up a guess as a value proposition would make this
exactly the document I'm trying not to write. What I know is that *I* want to know. If very
few people are like me, this is a good tool for a small number of people rather than a
product — fine, but I'd rather learn that on purpose than by accident. I can write tests for
every way a question comes out malformed, and I have. There's no test for whether anyone
wants this, which is why it's the risk that actually scares me.

**It spends your tokens on a question you might not answer.** The question gets written when
the session ends, on whatever model you already had selected — not a key you configure, just
the one you're already using. So the twenty seconds isn't quite the whole price.

I assumed this was a real objection and it mostly isn't. I measured it: across 107 of my own
sessions, 290MB of transcript contains 80KB of things I actually typed — everything else is
tool output and file dumps. Four in ten sessions contain no human input at all, so there's
nothing to ask about and nothing to spend. What costs something is the code, because a
question about *your* retry wrapper means reading it: around 36KB for a median session. Ten
thousand tokens, once, on a session you've already finished. Running the whole thing over a
real corpus since put a number on it — about 27 cents per session it decides to look at,
and it looks at well under half of them. I'd rather say that plainly than keep a caveat I'd
written before checking whether it was true.

**And it might just be nagging** — a sophisticated way of making you feel bad about things
you'd decided not to care about. It also has to survive the opposite: the reflex to swat it
away. If enter dismisses it in half a second, that reflex wins and the tool trains its own
banner blindness. The fix isn't to make skipping cost something — guilt kills these tools —
it's that the question has to earn the half-second by being *sharp*, not *alarming*, and
stay silent on the days there's nothing worth asking. Skipping is free and always valid,
and a question you never come for expires after a week rather than piling up into a backlog
you owe someone.

Pull delivery took most of the sting out of this: a tool that only speaks when you type its
name cannot nag you, which is one more thing the change of mind bought and I didn't plan
for. It also removes the only place the tool could have noticed it wasn't wanted. I still
want the version that reads three skips in a row and goes quiet for a week without a word —
not built — because a skip on a surface you deliberately opened means something a skip on
an interruption doesn't: you showed up and it wasted your time.

None of which touches day one. Wanting this still takes being willing to be wrong in front
of a computer, repeatedly, on purpose. Nobody has built a successful product around
voluntarily feeling stupid.

**And it only reaches people already in this editor.** grask lives in Claude Code and nowhere
else. I think that's right rather than limiting — the hollow-pattern problem is *worst*
exactly where the agent wrote the code, so that's where the tool belongs — but it caps who
this can ever help to people already working this way, and I'd rather call that a deliberate
choice than pretend it's universal. If the evidence ever says otherwise, that's the thing to
revisit.

## Where it's at

Alpha, and it works: sessions get read, most produce nothing, the ones that don't produce a
question you can answer in the terminal or in Claude Code. It has been pointed at a real
corpus rather than topics I type in by hand — because the way this dies is misreading code I
actually wrote, and a hand-fed topic is the one case where that can't happen.

Three things are missing, and they are the three that decide whether any of the above was
worth doing:

- **A way to know if the questions are good.** One keypress after the answer — *was this
  worth asking?* — and its yes-rate. Everything above is currently judged by me reading
  questions and deciding I like them, which is exactly the self-assessment this whole
  document says you can't trust.
- **Noticing the same gap twice.** Two sessions about the same concept produce two unrelated
  questions right now. A concept that keeps coming back and keeps grading wrong is the most
  useful thing this could ever tell anyone, and it can't see it.
- **Coming back a week later.** See above. Half a product without it.

Either I find out that being questioned changes what people actually understand, or I find
out that nobody wants to know what they don't. Both answers are worth having.

---

**What I want to know from you:**

- Do you recognize this? The pattern-in-production-you-can't-defend thing?
- Would you use it, honestly — or does "voluntarily find out what you don't know" sound
  like something you'd try twice and quietly stop?
- One question about the code you shipped today: useful, or the kind of thing you'd disable
  in week two? I've tried to make it cheap enough to survive, but I'm the wrong person to
  judge whether it is.
- Multiple choice, or a blank box? I picked four options because it deletes the model that
  grades you, and with it every way that model could flatter or falsely accuse you. The cost
  is that you might guess right and learn nothing. Wrong trade?
- Does something like this already exist and I've missed it?
