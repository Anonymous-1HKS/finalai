"""
ml/reinforcement.py
Reinforcement Learning (Q-Learning) cho điều phối đèn giao thông.
"""

import json, time, math, random, uuid, os, sys
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from collections import deque

# KHÔNG tự thêm sys.path ở đây — main.py đã thiết lập ROOT_DIR trước khi import
from database.db_manager import db


# ── STATE DISCRETIZATION ──────────────────────────────────────────────────────
def discretize_state(light_state: dict, road_state: dict) -> str:
    queue = min(5, light_state.get("queue_length", 0) // 3)
    speed = min(5, int(road_state.get("avg_speed", 60) / 15))
    phase = {"red": 0, "yellow": 1, "green": 2}.get(light_state.get("phase", "red"), 0)
    hour  = min(3, time.localtime().tm_hour // 6)
    jam   = min(3, int(road_state.get("jam_severity", 0.0) * 3))
    return f"{queue}_{speed}_{phase}_{hour}_{jam}"


# ── ACTIONS ───────────────────────────────────────────────────────────────────
class Action:
    HOLD             = 0
    SWITCH           = 1
    EXTEND_GREEN     = 2
    REDUCE_RED       = 3
    EMERGENCY_GREEN  = 4
    DISPATCH_POLICE  = 5


ALL_ACTIONS  = [0, 1, 2, 3, 4, 5]
ACTION_NAMES = {
    0: "Giữ pha",
    1: "Chuyển pha",
    2: "Kéo dài xanh +5s",
    3: "Rút ngắn đỏ -5s",
    4: "Bật xanh khẩn cấp",
    5: "Điều cảnh sát",
}


# ── REWARD ────────────────────────────────────────────────────────────────────
def compute_reward(prev_state: dict, next_state: dict, action: int,
                   had_incident: bool) -> float:
    reward = 0.0
    q_before = prev_state.get("queue_length", 0)
    q_after  = next_state.get("queue_length", 0)
    reward  += (q_before - q_after) * 0.5

    spd_before = prev_state.get("avg_speed", 0)
    spd_after  = next_state.get("avg_speed", 0)
    reward    += (spd_after - spd_before) * 0.1

    jam_after = next_state.get("jam_severity", 0.0)
    reward -= jam_after * 5.0

    if had_incident:
        reward -= 10.0
    if action == Action.SWITCH:
        reward -= 0.5
    if action == Action.EMERGENCY_GREEN and not next_state.get("emergency_vehicle"):
        reward -= 2.0

    return reward


# ── Q-TABLE AGENT ─────────────────────────────────────────────────────────────
class QTableAgent:
    def __init__(self, agent_id: str = "default",
                 alpha: float = 0.1, gamma: float = 0.95, epsilon: float = 0.2):
        self.agent_id      = agent_id
        self.alpha         = alpha
        self.gamma         = gamma
        self.epsilon       = epsilon
        self.q_table: Dict[str, List[float]] = {}
        self._episode_id   = str(uuid.uuid4())[:8]
        self._step_count   = 0
        self._total_reward = 0.0
        self._memory: deque = deque(maxlen=500)

    def _get_q(self, state: str) -> List[float]:
        if state not in self.q_table:
            self.q_table[state] = [0.0] * len(ALL_ACTIONS)
        return self.q_table[state]

    def select_action(self, state: str) -> int:
        if random.random() < self.epsilon:
            return random.choice(ALL_ACTIONS)
        qs = self._get_q(state)
        return qs.index(max(qs))

    def update(self, state: str, action: int, reward: float, next_state: str):
        q  = self._get_q(state)
        qn = self._get_q(next_state)
        q[action] = q[action] + self.alpha * (reward + self.gamma * max(qn) - q[action])
        self._memory.append((state, action, reward, next_state))
        self._total_reward += reward
        self._step_count   += 1
        self.epsilon = max(0.02, self.epsilon * 0.9999)
        try:
            db.save_ml_sample(
                features={"state": state, "action": action, "step": self._step_count},
                labels={"q_value": q[action], "reward": reward},
                source="rl_episode",
                episode_id=self._episode_id,
                reward=reward,
            )
        except Exception:
            pass

    def replay(self, batch_size: int = 16):
        if len(self._memory) < batch_size:
            return
        batch = random.sample(self._memory, batch_size)
        for (s, a, r, ns) in batch:
            self.update(s, a, r, ns)

    def load_from_db(self):
        try:
            samples = db.get_ml_training_data(source="rl_episode", limit=2000)
            for s in samples:
                state  = s["features"].get("state")
                action = s["features"].get("action")
                if state and action is not None:
                    q = self._get_q(state)
                    if len(q) == len(ALL_ACTIONS) and action < len(q):
                        q[action] = s["labels"].get("q_value", 0.0)
        except Exception:
            pass

    def save_to_db(self):
        try:
            db.save_ml_sample(
                features={"q_table_snapshot": json.dumps(self.q_table)[:4000]},
                labels={"episode": self._episode_id,
                        "total_reward": self._total_reward,
                        "steps": self._step_count,
                        "epsilon": self.epsilon},
                source="rl_snapshot",
                episode_id=self._episode_id,
            )
        except Exception:
            pass


# ── RL CONTROLLER ─────────────────────────────────────────────────────────────
class RLTrafficController:
    def __init__(self):
        self.agents: Dict[str, QTableAgent] = {}
        self._prev_states: Dict[str, dict]  = {}
        self._last_actions: Dict[str, int]  = {}

    def get_agent(self, light_id: str) -> QTableAgent:
        if light_id not in self.agents:
            agent = QTableAgent(agent_id=light_id)
            agent.load_from_db()
            self.agents[light_id] = agent
        return self.agents[light_id]

    def decide(self, light_id: str, light_state: dict, road_state: dict) -> dict:
        agent = self.get_agent(light_id)
        state = discretize_state(light_state, road_state)

        if light_id in self._prev_states:
            prev     = self._prev_states[light_id]
            prev_key = discretize_state(prev["light"], prev["road"])
            action   = self._last_actions.get(light_id, Action.HOLD)
            had_inc  = road_state.get("has_incident", False)
            reward   = compute_reward(prev["road"], road_state, action, had_inc)
            agent.update(prev_key, action, reward, state)
            agent.replay(batch_size=16)

        action = agent.select_action(state)
        self._prev_states[light_id]  = {"light": light_state, "road": road_state}
        self._last_actions[light_id] = action

        return {
            "light_id":    light_id,
            "action":      action,
            "action_name": ACTION_NAMES[action],
            "state_key":   state,
            "epsilon":     round(agent.epsilon, 3),
            "explanation": self._explain(action, light_state, road_state),
        }

    def _explain(self, action: int, ls: dict, rs: dict) -> str:
        q   = ls.get("queue_length", 0)
        spd = rs.get("avg_speed", 0)
        jam = rs.get("jam_severity", 0.0)
        return {
            Action.HOLD:            f"Hàng đợi={q} xe, tốc độ={spd:.0f} km/h, kẹt={jam:.0%} → giữ pha.",
            Action.SWITCH:          f"Hàng đợi tăng ({q} xe, kẹt={jam:.0%}) → chuyển pha.",
            Action.EXTEND_GREEN:    f"Lưu lượng cao, kẹt={jam:.0%} → kéo dài xanh +5s.",
            Action.REDUCE_RED:      f"Hàng đợi lớn ({q} xe) → rút ngắn đỏ -5s.",
            Action.EMERGENCY_GREEN: "Xe ưu tiên phát hiện → bật xanh khẩn cấp.",
            Action.DISPATCH_POLICE: f"Phát hiện kẹt cứng (kẹt={jam:.0%}) → điều cảnh sát.",
        }.get(action, "")

    def apply_action(self, action: int, light) -> Optional[str]:
        """Áp dụng action lên TrafficLight object. Trả về mô tả hoặc None."""
        try:
            from OOP.traffic_lights import LightPhase
        except ImportError:
            return None

        if action == Action.HOLD:
            return None
        elif action == Action.SWITCH:
            light._advance_phase()
            return f"RL chuyển pha → {light.phase.value}"
        elif action == Action.EXTEND_GREEN:
            if light.phase == LightPhase.GREEN:
                light.countdown += 5
                return "RL kéo dài xanh +5s"
        elif action == Action.REDUCE_RED:
            if light.phase == LightPhase.RED:
                light.countdown = max(5, light.countdown - 5)
                return "RL rút ngắn đỏ -5s"
        elif action == Action.EMERGENCY_GREEN:
            light.preempt(duration=15)
            return "RL bật xanh khẩn cấp 15s"
        elif action == Action.DISPATCH_POLICE:
            return "DISPATCH_POLICE"
        return None

    def save_all(self):
        for agent in self.agents.values():
            agent.save_to_db()

    def get_stats(self) -> dict:
        return {
            lid: {
                "epsilon":      round(a.epsilon, 3),
                "steps":        a._step_count,
                "total_reward": round(a._total_reward, 1),
                "states_known": len(a.q_table),
            }
            for lid, a in self.agents.items()
        }