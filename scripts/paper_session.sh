#!/usr/bin/env bash
# Launch, stop and inspect the unattended live paper session (ADR-0051).
#
# One already-tested command (docs/monday-divergence-run.md) plus the three things
# an overnight run needs and the CLI does not provide:
#
#   * a --out that is unique per launch. ADR-0048 truncates fill_divergence.csv at
#     session start, so a retry into an occupied directory destroys exactly the
#     partial rows ADR-0048 exists to preserve.
#   * a detached launch. SIGHUP is not handled (ADR-0043's stated gap), so a closed
#     terminal kills the run.
#   * a stop that is SIGTERM and never SIGKILL, so the session finalizes (ADR-0043).
#
# The command itself is never rewritten here: it is echoed in full at every launch
# and written to <out>/launch.cmd, and the runbook carries the same text by hand.
set -euo pipefail

# --- The run, exactly as the runbook states it -------------------------------
# Overridable so a retry can change strategy/symbols/interval; the rest (source,
# broker, feed, --live, --divergence) is what the run *is* and is not a knob.
PAPER_STRATEGY="${PAPER_STRATEGY:-sma_crossover}"
PAPER_SYMBOLS="${PAPER_SYMBOLS:-@blue20}"
PAPER_INTERVAL="${PAPER_INTERVAL:-5m}"
PAPER_DATE="${PAPER_DATE:-2026-08-10}"
# Which market the session trades (ADR-0057): calendar, completeness policy, risk
# posture and cost model all come from this one flag. `us_equity` reproduces every
# launch this script made before it existed.
PAPER_MARKET="${PAPER_MARKET:-us_equity}"
# ADR-0034's IEX tape is EQUITY-ONLY (ADR-0058 §9): `CryptoBarsRequest` has no
# `feed` field, so `RealAlpacaClient` *refuses* feed-plus-crypto at construction.
# A hardcoded `--data-feed iex` therefore made `--market crypto` unlaunchable from
# here -- the run died before any network call. The CLI already selects `iex` for a
# live equity session by itself, so this default is the runbook's text rather than
# a behavioural knob; empty means "let the CLI decide".
# Crypto CLEARS it unconditionally rather than defaulting it away: a feed on the
# crypto venue is always an error, so the launcher must never be able to emit one.
case "$PAPER_MARKET" in
crypto | crypto_24_7) PAPER_DATA_FEED="" ;;
*) PAPER_DATA_FEED="${PAPER_DATA_FEED:-iex}" ;;
esac
PAPER_OUT_ROOT="${PAPER_OUT_ROOT:-results/paper}"
PAPER_ENV_FILE="${PAPER_ENV_FILE:-.env}"
PAPER_LABEL="${PAPER_LABEL:-}"
PAPER_EXTRA_ARGS="${PAPER_EXTRA_ARGS:-}"
# auto (tmux if installed, else setsid) | tmux | setsid. Both detached; `auto` is
# what an operator wants and the explicit values are how the fallback gets tested.
PAPER_LAUNCHER="${PAPER_LAUNCHER:-auto}"
# How long `stop` waits for finalization before reporting it is still going.
PAPER_STOP_TIMEOUT="${PAPER_STOP_TIMEOUT:-60}"

POINTER="$PAPER_OUT_ROOT/.last-launch"
ARTIFACTS=(paper_session.log paper_state.json equity_curve.csv result.json fill_divergence.csv)

die() {
	echo "error: $*" >&2
	exit 1
}

# Build the invocation into the CMD array. `uv run --env-file` errors on a missing
# file, and the keys may already be exported, so the flag is conditional.
build_cmd() {
	local out="$1"
	CMD=(uv run)
	if [ -n "$PAPER_ENV_FILE" ] && [ -f "$PAPER_ENV_FILE" ]; then
		CMD+=(--env-file "$PAPER_ENV_FILE")
	fi
	CMD+=(
		trading paper
		--strategy "$PAPER_STRATEGY"
		--symbols "$PAPER_SYMBOLS"
		--interval "$PAPER_INTERVAL"
		--market "$PAPER_MARKET"
		--source alpaca
		--broker alpaca
		--live
		--divergence
		--from "$PAPER_DATE"
		--to "$PAPER_DATE"
		--out "$out"
	)
	if [ -n "$PAPER_DATA_FEED" ]; then
		CMD+=(--data-feed "$PAPER_DATA_FEED")
	fi
	if [ -n "$PAPER_EXTRA_ARGS" ]; then
		# Deliberately word-split: this carries flags like `--max-empty-polls 1`.
		read -r -a extra <<<"$PAPER_EXTRA_ARGS"
		CMD+=("${extra[@]}")
	fi
}

new_out_dir() {
	local stamp
	stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
	OUT="$PAPER_OUT_ROOT/$stamp-$PAPER_LABEL"
	STAMP="$stamp"
	[ -e "$OUT" ] && die "$OUT already exists -- refusing to reuse it (ADR-0048 truncates fill_divergence.csv at session start)"
	mkdir -p "$OUT"
}

show_cmd() {
	printf '  '
	printf '%q ' "${CMD[@]}"
	printf '\n'
}

resolve_out() {
	OUT="${PAPER_OUT:-}"
	if [ -z "$OUT" ] && [ -f "$POINTER" ]; then
		OUT="$(cat "$POINTER")"
	fi
}

list_artifacts() {
	local out="$1" name
	for name in "${ARTIFACTS[@]}" console.log; do
		if [ -f "$out/$name" ]; then
			printf '  %-22s %8s bytes\n' "$name" "$(wc -c <"$out/$name" | tr -d ' ')"
		else
			printf '  %-22s %8s\n' "$name" "not written"
		fi
	done
}

# --- launch: detached, unique --out ------------------------------------------
cmd_launch() {
	PAPER_LABEL="${PAPER_LABEL:-divergence}"
	new_out_dir
	build_cmd "$OUT"

	echo "Launching the live paper session. The command:"
	show_cmd
	echo
	show_cmd >"$OUT/launch.cmd"

	local mode session pid use_tmux=no
	case "$PAPER_LAUNCHER" in
	auto) command -v tmux >/dev/null 2>&1 && use_tmux=yes ;;
	tmux)
		command -v tmux >/dev/null 2>&1 || die "PAPER_LAUNCHER=tmux but tmux is not installed"
		use_tmux=yes
		;;
	setsid | nohup) use_tmux=no ;;
	*) die "PAPER_LAUNCHER must be auto, tmux or setsid (got '$PAPER_LAUNCHER')" ;;
	esac

	if [ "$use_tmux" = yes ]; then
		mode=tmux
		session="paper-$STAMP"
		# tmux owns the process, so closing this terminal cannot reach it: the
		# session's processes live under the tmux server, in their own session
		# with no controlling terminal of ours.
		tmux new-session -d -s "$session" -- "${CMD[@]}"
		# Mirror the pane to a file without inserting a process into the signal
		# path: pipe-pane copies output, so the tree stays tmux -> uv -> python.
		tmux pipe-pane -t "$session" -o "cat >> $(printf '%q' "$PWD/$OUT/console.log")"
		pid="$(tmux list-panes -t "$session" -F '#{pane_pid}' | head -n 1)"
	else
		mode=setsid
		session=""
		# Put the session in a session id of its own with no controlling terminal,
		# so a closed terminal cannot reach it at all.
		#
		# `nohup` alone is NOT enough here, measured rather than assumed: `uv run`
		# installs its own SIGHUP handler (SigCgt carries SIGHUP), which overrides
		# the SIG_IGN nohup sets, and a HUP arriving in uv's first second -- which
		# is exactly what closing the terminal straight after launching does --
		# kills the wrapper before the python child exists. Observed: the process
		# gone with a zero-byte console.log. Once the child is up it does inherit
		# the ignore and survives a HUP, so nohup fails only in the window an
		# impatient operator is most likely to hit.
		#
		# The pid comes from the child writing its own $$ before exec'ing, because
		# `setsid` may or may not fork (it forks only when the caller is already a
		# process-group leader), so $! is not reliably the wrapper.
		local pidfile="$OUT/session.pid" waited=0
		setsid bash -c 'echo $$ >"$1"; shift; exec "$@"' _ "$pidfile" "${CMD[@]}" \
			>"$OUT/console.log" 2>&1 </dev/null &
		disown || true
		while [ ! -s "$pidfile" ] && [ "$waited" -lt 50 ]; do
			sleep 0.1
			waited=$((waited + 1))
		done
		pid="$(cat "$pidfile" 2>/dev/null || true)"
		[ -n "$pid" ] || die "launched, but could not read the session pid from $pidfile"
	fi

	{
		echo "MODE=$mode"
		echo "SESSION=$session"
		echo "PID=$pid"
		echo "OUT=$OUT"
		echo "STARTED_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	} >"$OUT/launch.env"
	echo "$OUT" >"$POINTER"

	if [ "$mode" = tmux ]; then
		echo "Launched under tmux session '$session' (pid $pid -- the uv wrapper)."
		echo "  Reattach:  tmux attach -t $session"
	else
		echo "Launched detached with setsid (pid $pid) -- its own session, no controlling"
		echo "terminal, so a closed terminal cannot reach it. No scrollback to reattach to;"
		echo "read the console log instead."
		command -v tmux >/dev/null 2>&1 ||
			echo "  (tmux is not installed; with it you would get a reattachable session.)"
	fi
	echo "  Console:   $OUT/console.log"
	echo "  Artifacts: $OUT/"
	echo "  Watch:     make paper-status"
	echo "  Stop:      make paper-stop        (SIGTERM -> finalizes, ADR-0043)"
	echo
	echo "It is safe to close this terminal now. It is NOT safe to let the machine sleep."
}

# --- dryrun: the same command, foreground, scratch --out ---------------------
cmd_dryrun() {
	PAPER_LABEL="${PAPER_LABEL:-dryrun}"
	new_out_dir
	build_cmd "$OUT"

	echo "Dry run: the Monday command, into a scratch --out. The command:"
	show_cmd
	show_cmd >"$OUT/launch.cmd"
	echo
	echo "With the venue shut this is what SUCCESS looks like:"
	echo "  * a 'Warmup: primed N completed bar(s)' line with N > 0 (ADR-0042/0047)"
	echo "  * 0 bars processed and 0 orders -- warmup is data, never trades"
	echo "  * an exit on the silence tolerance, with all five artifacts written"
	echo "It waits for one poll boundary, so at --interval $PAPER_INTERVAL expect up to 5 minutes."
	echo

	set +e
	"${CMD[@]}" 2>&1 | tee "$OUT/console.log"
	local rc=${PIPESTATUS[0]}
	set -e

	echo
	echo "--- dry run verdict ---"
	echo "exit code: $rc"
	local warmup
	warmup="$(grep -m1 'Warmup: primed' "$OUT/console.log" || true)"
	if [ -n "$warmup" ]; then
		echo "warmup:    $warmup"
	else
		echo "warmup:    NOT SEEN -- the session primed nothing. Do not start the real run."
	fi
	echo "artifacts in $OUT:"
	list_artifacts "$OUT"
	echo "(paper_state.json is written per processed bar, so with a shut venue and 0 live"
	echo " bars it is absent here. On Monday it appears with the first bar.)"
	if [ -n "$warmup" ] && [ "$rc" -eq 0 ]; then
		echo "PASS: the path works end to end. Scratch output above is throwaway."
	else
		echo "FAIL: see above."
		return 1
	fi
}

# --- stop: SIGTERM, never SIGKILL --------------------------------------------
cmd_stop() {
	resolve_out
	if [ -z "$OUT" ] || [ ! -f "$OUT/launch.env" ]; then
		echo "Nothing to stop: no launch recorded (looked for $POINTER)."
		echo "If you started the session by hand, stop it with: kill <pid>   (never kill -9)"
		return 0
	fi
	# shellcheck disable=SC1091
	MODE=""
	SESSION=""
	PID=""
	. "$OUT/launch.env"

	# `uv run` forks, so the pid we recorded is the *wrapper*. Signalling it is
	# correct and verified: uv forwards SIGTERM and the session finalizes. Under
	# tmux the pane's live pid is the same wrapper and is the more reliable
	# source, so prefer it when the session still exists.
	local pid="$PID"
	if [ "$MODE" = tmux ] && command -v tmux >/dev/null 2>&1 &&
		tmux has-session -t "$SESSION" 2>/dev/null; then
		pid="$(tmux list-panes -t "$SESSION" -F '#{pane_pid}' | head -n 1)"
	fi

	if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
		echo "Nothing running: pid ${pid:-unknown} has already exited."
		echo "Artifacts in $OUT:"
		list_artifacts "$OUT"
		return 0
	fi

	# Refuse to signal a pid that has been recycled onto something else.
	local cmdline=""
	if [ -r "/proc/$pid/cmdline" ]; then
		cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
		case "$cmdline" in
		*"trading"*paper*) ;;
		*) die "pid $pid is not our session (cmdline: $cmdline) -- refusing to signal it" ;;
		esac
	fi

	echo "Stopping pid $pid with SIGTERM (never SIGKILL: -9 cannot be caught, so the"
	echo "session would lose everything ADR-0043 finalizes -- and uv cannot forward it)."
	kill -TERM "$pid"

	local waited=0
	while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$PAPER_STOP_TIMEOUT" ]; do
		sleep 1
		waited=$((waited + 1))
	done

	if kill -0 "$pid" 2>/dev/null; then
		echo "Still running after ${waited}s. A session inside a poll sleep can take up to one"
		echo "bar interval to notice; finalization itself takes milliseconds. Re-run this to check."
		return 1
	fi
	echo "Stopped after ${waited}s. Artifacts in $OUT:"
	list_artifacts "$OUT"
	if [ "$MODE" = tmux ] && command -v tmux >/dev/null 2>&1; then
		tmux kill-session -t "$SESSION" 2>/dev/null || true
	fi
}

# --- status: tail-shaped visibility, nothing more ----------------------------
cmd_status() {
	resolve_out
	if [ -z "$OUT" ] || [ ! -f "$OUT/launch.env" ]; then
		echo "No launch recorded (looked for $POINTER)."
		return 0
	fi
	MODE=""
	SESSION=""
	PID=""
	STARTED_UTC=""
	. "$OUT/launch.env"

	local pid="$PID"
	if [ "$MODE" = tmux ] && command -v tmux >/dev/null 2>&1 &&
		tmux has-session -t "$SESSION" 2>/dev/null; then
		pid="$(tmux list-panes -t "$SESSION" -F '#{pane_pid}' | head -n 1)"
	fi
	local state="exited"
	kill -0 "$pid" 2>/dev/null && state="running"

	echo "Run:     $OUT"
	echo "Started: $STARTED_UTC   (mode $MODE${SESSION:+, tmux session $SESSION})"
	echo "State:   $state (pid $pid)"
	echo
	echo "Artifacts:"
	list_artifacts "$OUT"
	if [ -f "$OUT/paper_state.json" ]; then
		echo
		echo "paper_state.json:"
		sed 's/^/  /' "$OUT/paper_state.json"
	fi
	if [ -f "$OUT/console.log" ]; then
		echo
		echo "Last 15 console lines:"
		tail -n 15 "$OUT/console.log" | sed 's/^/  /'
	fi
}

case "${1:-}" in
launch) cmd_launch ;;
dryrun) cmd_dryrun ;;
stop) cmd_stop ;;
status) cmd_status ;;
*) die "usage: $0 {launch|dryrun|stop|status}" ;;
esac
