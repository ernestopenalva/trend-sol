"""Closed-snapshot exit telemetry only; never evaluates an economic rule."""
from copy import deepcopy
from datetime import datetime


class ExitContextTelemetry:
    def on_closed_5m(self, snapshot):
        if not self.enabled or not snapshot:
            return
        values = snapshot.get('tf_5m') or {}
        closed = values.get('latest_closed_at_ms')
        captured = snapshot.get('captured_at')
        if closed is None or values.get('closed') is False:
            return
        if captured and closed > datetime.fromisoformat(captured.replace('Z', '+00:00')).timestamp()*1000:
            return
        self._remember_exit_context(self.latest_market_context)
        self._remember_exit_context(snapshot)
        prior = (self.latest_market_context or {}).get('tf_5m') or {}
        if (prior.get('latest_closed_at_ms') or -1) > closed:
            return
        self.latest_market_context = deepcopy(snapshot)
        self._save_state()

    def _remember_exit_context(self, snapshot):
        values = (snapshot or {}).get('tf_5m') or {}
        closed = values.get('latest_closed_at_ms')
        if closed is None or values.get('closed') is False:
            return
        self._exit_context_history = sorted(
            [s for s in self._exit_context_history if s['tf_5m']['latest_closed_at_ms'] != closed]
            + [deepcopy(snapshot)], key=lambda s:s['tf_5m']['latest_closed_at_ms'],
        )[-6:]

    def _exit_context_at(self, moment):
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment.replace('Z', '+00:00'))
        stamp = moment.timestamp()*1000
        eligible = [s for s in [*self._exit_context_history, self.latest_market_context]
                    if s and (s.get('tf_5m') or {}).get('closed') is not False
                    and (s.get('tf_5m') or {}).get('latest_closed_at_ms') is not None
                    and s['tf_5m']['latest_closed_at_ms'] <= stamp]
        return deepcopy(max(eligible,key=lambda s:s['tf_5m']['latest_closed_at_ms'])) if eligible else None
