"""
model.v3.1 - 修复版：每个设备独立决策，互不竞争
核心修改：
1. 每个设备有独立的 reward 分量
2. 成本/频率惩罚归属到设备自己
3. 关闭动作有机会成本
4. Critic 分解为设备级价值函数
"""

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.distributions import Categorical
import numpy as np
import os, pickle, sys

torch.manual_seed(42)
np.random.seed(42)

# ============= 环境（保持不变）=============
class UserStateSimulator:
    """智能座舱情绪调节环境"""
    
    def __init__(self):
        self.emotion_keys = ['平静', '恐惧', '惊讶', '开心', '愤怒', '轻蔑', '悲伤', '厌恶']
        self.n_emotions = len(self.emotion_keys)
        
        self.optimal_emotion_ranges = {
            '平静': (0.30, 0.50), '恐惧': (0.00, 0.05), '惊讶': (0.00, 0.10),
            '开心': (0.25, 0.45), '愤怒': (0.00, 0.05), '轻蔑': (0.00, 0.03),
            '悲伤': (0.00, 0.05), '厌恶': (0.00, 0.03)
        }
        self.optimal_fatigue_range = (15, 35)
        
        self.device_actions = {
            'music': ['镇静', '提振', '稳定', '共情', '专注', '关闭'],
            'ac': ['轻微降温', '轻微升温', '维持不变', '增强送风', '减弱送风', '关闭'],
            'light': ['提亮冷光', '提亮暖光', '降暗暖光', '降暗冷光', '柔和动态', '维持不变', '关闭'],
            'aroma': ['舒缓', '平静', '提神', '关闭'],
            'massage': ['轻柔颈肩放松', '稳定背部舒缓', '全身恢复', '深层缓解', '轻度提神', '关闭']
        }
        
        self.device_names = ['music', 'ac', 'light', 'aroma', 'massage']
        self.n_devices = len(self.device_names)
        
        self.device_state = {dev: '关闭' for dev in self.device_names}
        
        self.device_cooldown_times = {
            'music': 3, 'ac': 4, 'light': 2, 'aroma': 5, 'massage': 6
        }
        self.device_cooldowns = {dev: 0 for dev in self.device_names}
        self.action_usage_count = {dev: {} for dev in self.device_names}
        
        self.action_effects = self._design_balanced_effects()
        
        self.device_state_dims = {dev: len(actions) for dev, actions in self.device_actions.items()}
        self.n_states = self.n_emotions + 1 + sum(self.device_state_dims.values())
    
    def _design_balanced_effects(self):
        effects = {}
        
        effects['music'] = {
            0: {'emotions': [0.06, -0.03, -0.01, -0.01, -0.03, -0.01, -0.02, -0.01], 'fatigue': -2.0, 'cost': 0.03},
            1: {'emotions': [-0.03, 0.01, 0.03, 0.06, 0.01, 0.0, -0.02, 0.0], 'fatigue': 2.5, 'cost': 0.04},
            2: {'emotions': [0.04, -0.01, 0.0, 0.02, -0.01, 0.0, -0.01, 0.0], 'fatigue': -1.0, 'cost': 0.02},
            3: {'emotions': [0.02, 0.0, 0.0, 0.02, -0.03, -0.02, 0.01, 0.0], 'fatigue': -0.5, 'cost': 0.03},
            4: {'emotions': [0.05, -0.01, -0.02, 0.0, 0.0, 0.0, 0.0, 0.0], 'fatigue': 3.0, 'cost': 0.05},
            5: {'emotions': [0.0]*8, 'fatigue': 0.0, 'cost': 0.0}
        }
        
        effects['ac'] = {
            0: {'emotions': [0.02, -0.01, 0.0, 0.01, -0.02, 0.0, 0.0, -0.01], 'fatigue': -3.0, 'cost': 0.4},
            1: {'emotions': [0.03, -0.01, 0.0, 0.01, -0.01, 0.0, -0.01, 0.0], 'fatigue': 1.5, 'cost': 0.3},
            2: {'emotions': [0.0]*8, 'fatigue': 0.0, 'cost': 0.0},
            3: {'emotions': [-0.02, 0.01, 0.01, 0.0, 0.01, 0.0, 0.0, 0.01], 'fatigue': -4.5, 'cost': 0.6},
            4: {'emotions': [0.01, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 'fatigue': -1.5, 'cost': 0.2},
            5: {'emotions': [0.0]*8, 'fatigue': 0.0, 'cost': 0.0}
        }
        
        effects['light'] = {
            0: {'emotions': [0.01, 0.01, 0.01, 0.0, 0.01, 0.0, -0.01, 0.0], 'fatigue': -2.5, 'cost': 0.3},
            1: {'emotions': [0.03, -0.01, 0.0, 0.02, -0.01, 0.0, -0.01, 0.0], 'fatigue': -1.5, 'cost': 0.2},
            2: {'emotions': [0.04, -0.02, -0.01, 0.01, -0.02, -0.01, -0.02, -0.01], 'fatigue': -3.5, 'cost': 0.4},
            3: {'emotions': [0.01, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 'fatigue': -2.0, 'cost': 0.2},
            4: {'emotions': [0.02, -0.01, 0.0, 0.01, -0.01, 0.0, -0.01, 0.0], 'fatigue': -1.0, 'cost': 0.3},
            5: {'emotions': [0.0]*8, 'fatigue': 0.0, 'cost': 0.0},
            6: {'emotions': [0.0]*8, 'fatigue': 0.0, 'cost': 0.0}
        }
        
        effects['aroma'] = {
            0: {'emotions': [0.05, -0.03, 0.0, 0.02, -0.03, -0.02, -0.03, -0.02], 'fatigue': -4.0, 'cost': 0.8},
            1: {'emotions': [0.06, -0.02, -0.01, 0.01, -0.02, -0.01, -0.02, -0.01], 'fatigue': -2.5, 'cost': 0.6},
            2: {'emotions': [-0.02, 0.0, 0.02, 0.03, 0.0, 0.0, -0.01, 0.0], 'fatigue': -5.0, 'cost': 0.7},
            3: {'emotions': [0.0]*8, 'fatigue': 0.0, 'cost': 0.0}
        }
        
        effects['massage'] = {
            0: {'emotions': [0.03, -0.01, 0.0, 0.02, -0.02, 0.0, -0.01, 0.0], 'fatigue': -4.0, 'cost': 1.0},
            1: {'emotions': [0.04, -0.02, 0.0, 0.03, -0.02, -0.01, -0.02, -0.01], 'fatigue': -5.5, 'cost': 1.2},
            2: {'emotions': [0.05, -0.02, 0.0, 0.04, -0.02, -0.01, -0.03, -0.01], 'fatigue': -8.0, 'cost': 1.8},
            3: {'emotions': [0.06, -0.03, 0.0, 0.05, -0.03, -0.01, -0.04, -0.02], 'fatigue': -10.0, 'cost': 2.5},
            4: {'emotions': [0.01, 0.0, 0.01, 0.04, 0.0, 0.0, -0.01, 0.0], 'fatigue': -6.0, 'cost': 1.1},
            5: {'emotions': [0.0]*8, 'fatigue': 0.0, 'cost': 0.0}
        }
        
        return effects
    
    def reset_device_states(self):
        self.device_state = {dev: '关闭' for dev in self.device_names}
        self.device_cooldowns = {dev: 0 for dev in self.device_names}
        self.action_usage_count = {dev: {} for dev in self.device_names}
    
    def encode_device_states(self):
        encoding = []
        for dev in self.device_names:
            states = self.device_actions[dev]
            current = self.device_state[dev]
            vec = [1.0 if current == s else 0.0 for s in states]
            encoding.extend(vec)
        return np.array(encoding, dtype=np.float32)
    
    def generate_random_initial_state(self):
        self.reset_device_states()
        raw_emotions = np.random.dirichlet(np.ones(self.n_emotions))
        fatigue = np.random.uniform(0, 100)
        base_state = np.concatenate([raw_emotions, [fatigue]]).astype(np.float32)
        device_encoding = self.encode_device_states()
        return np.concatenate([base_state, device_encoding])
    
    def compute_ideal_state(self, initial_state):
        ideal = np.zeros(self.n_states, dtype=np.float32)
        current_emotions = initial_state[:self.n_emotions]
        
        for i, key in enumerate(self.emotion_keys):
            opt_min, opt_max = self.optimal_emotion_ranges[key]
            opt_center = (opt_min + opt_max) / 2.0
            current = current_emotions[i]
            
            if opt_min <= current <= opt_max:
                ideal[i] = current * 0.7 + opt_center * 0.3
            elif current < opt_min:
                gap = opt_min - current
                ideal[i] = current + gap * 0.6
            else:
                gap = current - opt_max
                ideal[i] = current - gap * 0.6
            ideal[i] += np.random.randn() * 0.01
        
        ideal[:self.n_emotions] = np.clip(ideal[:self.n_emotions], 0, 1)
        ideal[:self.n_emotions] /= (ideal[:self.n_emotions].sum() + 1e-8)
        
        current_fatigue = initial_state[self.n_emotions]
        opt_min, opt_max = self.optimal_fatigue_range
        opt_center = (opt_min + opt_max) / 2.0
        
        if opt_min <= current_fatigue <= opt_max:
            ideal[self.n_emotions] = current_fatigue * 0.7 + opt_center * 0.3
        elif current_fatigue < opt_min:
            gap = opt_min - current_fatigue
            ideal[self.n_emotions] = current_fatigue + gap * 0.6
        else:
            gap = current_fatigue - opt_max
            ideal[self.n_emotions] = current_fatigue - gap * 0.6
        
        ideal[self.n_emotions] += np.random.randn() * 1.0
        ideal[self.n_emotions] = np.clip(ideal[self.n_emotions], 0, 100)
        ideal[self.n_emotions + 1:] = initial_state[self.n_emotions + 1:]
        
        return ideal
    
    def state_transition(self, state, device_actions_dict):
        """返回每个设备的独立效果"""
        new_state = state.copy()
        device_effects = {}  # 新增：记录每个设备的独立效果
        
        for dev, action_idx in device_actions_dict.items():
            if self.device_cooldowns[dev] > 0:
                self.device_cooldowns[dev] -= 1
                device_effects[dev] = {
                    'emotion_change': np.zeros(self.n_emotions),
                    'fatigue_change': 0.0,
                    'cost': 0.0,
                    'active': False
                }
                continue
            
            action_name = self.device_actions[dev][action_idx]
            
            if action_name in ['关闭', '维持不变']:
                self.device_state[dev] = action_name
                device_effects[dev] = {
                    'emotion_change': np.zeros(self.n_emotions),
                    'fatigue_change': 0.0,
                    'cost': 0.0,
                    'active': False
                }
                continue
            
            effects = self.action_effects[dev][action_idx]
            usage_count = self.action_usage_count[dev].get(action_idx, 0)
            decay_factor = 0.92 ** usage_count
            
            emotion_change = np.array(effects['emotions']) * decay_factor
            fatigue_change = effects['fatigue'] * decay_factor
            cost = effects['cost']
            
            # 记录该设备的独立效果
            device_effects[dev] = {
                'emotion_change': emotion_change,
                'fatigue_change': fatigue_change,
                'cost': cost,
                'active': True
            }
            
            # 应用到总状态
            new_state[:self.n_emotions] += emotion_change
            new_state[self.n_emotions] += fatigue_change
            
            self.device_state[dev] = action_name
            self.device_cooldowns[dev] = self.device_cooldown_times[dev]
            self.action_usage_count[dev][action_idx] = usage_count + 1
        
        new_state[:self.n_emotions] = np.clip(new_state[:self.n_emotions], 0, 1)
        emotion_sum = new_state[:self.n_emotions].sum()
        if emotion_sum > 0:
            new_state[:self.n_emotions] /= emotion_sum
        
        new_state[self.n_emotions] = np.clip(new_state[self.n_emotions], 0, 100)
        new_state[self.n_emotions + 1:] = self.encode_device_states()
        
        for dev in self.device_names:
            if dev not in device_actions_dict and self.device_cooldowns[dev] > 0:
                self.device_cooldowns[dev] -= 1
        
        return new_state, device_effects
    
    def generate_dataset(self, n_samples=5000, save_path='data/UserState_fixed.pkl'):
        print("="*70)
        print(f"生成训练数据集: {n_samples} 条")
        print("="*70)
        
        dataset = []
        for i in range(n_samples):
            initial_state = self.generate_random_initial_state()
            ideal_state = self.compute_ideal_state(initial_state)
            dataset.append({'initial_state': initial_state, 'ideal_state': ideal_state})
            
            if (i + 1) % 1000 == 0:
                print(f"  已生成 {i+1}/{n_samples}")
        
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, 'wb') as f:
            pickle.dump(dataset, f)
        
        print(f" 数据集已保存: {save_path}\n")
        return dataset


# =============  核心修改：分解式 Actor-Critic =============
class FactorizedActorCritic(nn.Module):
    """每个设备有独立的 Actor 和 Critic"""
    def __init__(self, state_dim, device_actions, hidden_dim=256):
        super(FactorizedActorCritic, self).__init__()
        
        self.device_names = list(device_actions.keys())
        self.device_actions = device_actions
        
        # 共享编码器
        self.state_encoder = nn.Sequential(
            nn.Linear(state_dim * 2, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU()
        )
        
        #  修改1：每个设备独立的 Actor
        self.actors = nn.ModuleDict()
        for dev in self.device_names:
            n_actions = len(device_actions[dev])
            self.actors[dev] = nn.Sequential(
                nn.Linear(hidden_dim, hidden_dim // 2),
                nn.ReLU(),
                nn.Dropout(0.1),
                nn.Linear(hidden_dim // 2, n_actions)
            )
        
        #  修改2：每个设备独立的 Critic（估计该设备的价值）
        self.critics = nn.ModuleDict()
        for dev in self.device_names:
            self.critics[dev] = nn.Sequential(
                nn.Linear(hidden_dim, hidden_dim // 2),
                nn.ReLU(),
                nn.Dropout(0.1),
                nn.Linear(hidden_dim // 2, 1)
            )
    
    def forward(self, current_state, ideal_state):
        combined = torch.cat([current_state, ideal_state], dim=-1)
        features = self.state_encoder(combined)
        
        device_probs = {}
        device_values = {}
        
        for dev in self.device_names:
            logits = self.actors[dev](features)
            probs = F.softmax(logits, dim=-1)
            device_probs[dev] = probs
            
            value = self.critics[dev](features).squeeze(-1)
            device_values[dev] = value
        
        return device_probs, device_values


# =============  核心修改：PPO Trainer =============
class FactorizedPPOTrainer:
    def __init__(self, simulator, device='cpu', hidden_dim=256, lr=3e-4):
        self.device = device
        self.simulator = simulator
        
        self.policy = FactorizedActorCritic(
            state_dim=simulator.n_states,
            device_actions=simulator.device_actions,
            hidden_dim=hidden_dim
        ).to(device)
        
        self.optimizer = optim.Adam(self.policy.parameters(), lr=lr)
        
        self.gamma = 0.99
        self.gae_lambda = 0.95
        self.clip_epsilon = 0.2
        self.value_coef = 0.5
        self.entropy_coef = 0.02
        
        self.max_steps = 25
        self.success_threshold = 0.18
        
        self.episode_rewards = []
        self.device_policy_losses = {dev: [] for dev in simulator.device_names}
        self.device_value_losses = {dev: [] for dev in simulator.device_names}
    
    def normalize_state(self, state):
        normalized = state.copy()
        n_emo = self.simulator.n_emotions
        normalized[n_emo] = state[n_emo] / 100.0
        return normalized
    
    def compute_device_reward(self, dev, current_state, next_state, ideal_state, device_effect):
        """ 修改3：每个设备独立的奖励函数"""
        n_emo = self.simulator.n_emotions
        
        if not device_effect['active']:
            #  修改4：关闭有机会成本（如果当前距离理想状态还很远）
            curr_distance = np.linalg.norm(
                self.normalize_state(current_state)[:n_emo+1] - 
                self.normalize_state(ideal_state)[:n_emo+1]
            )
            if curr_distance > 0.3:
                return -0.5  # 机会成本惩罚
            return 0.0
        
        # 计算该设备带来的改善
        prev_distance = np.linalg.norm(
            self.normalize_state(current_state)[:n_emo+1] - 
            self.normalize_state(ideal_state)[:n_emo+1]
        )
        curr_distance = np.linalg.norm(
            self.normalize_state(next_state)[:n_emo+1] - 
            self.normalize_state(ideal_state)[:n_emo+1]
        )
        improvement = prev_distance - curr_distance
        
        reward = 0.0
        
        # 改善奖励（只看自己的贡献）
        if improvement > 0:
            reward += improvement * 15.0
        else:
            reward += improvement * 4.0
        
        #  修改5：成本归属到设备自己
        reward -= device_effect['cost']
        
        #  修改6：频率惩罚归属到设备自己
        usage_count = self.simulator.action_usage_count[dev].get(
            list(self.simulator.action_usage_count[dev].keys())[-1] 
            if self.simulator.action_usage_count[dev] else 0, 
            0
        )
        if usage_count > 3:
            reward -= 0.3 * np.log(usage_count - 2)
        
        return float(np.clip(reward, -10.0, 10.0))
    
    def collect_trajectory(self, initial_state, ideal_state, max_steps=25):
        trajectory = []
        current_state = initial_state.copy()
        self.simulator.reset_device_states()
        
        for step in range(max_steps):
            current_norm = torch.FloatTensor(self.normalize_state(current_state)).unsqueeze(0).to(self.device)
            ideal_norm = torch.FloatTensor(self.normalize_state(ideal_state)).unsqueeze(0).to(self.device)
            
            with torch.no_grad():
                device_probs, device_values = self.policy(current_norm, ideal_norm)
                
                device_actions = {}
                device_log_probs = {}
                
                for dev in self.simulator.device_names:
                    probs = device_probs[dev]
                    dist = Categorical(probs)
                    action = dist.sample()
                    log_prob = dist.log_prob(action)
                    
                    device_actions[dev] = int(action.item())
                    device_log_probs[dev] = log_prob.detach()
            
            next_state, device_effects = self.simulator.state_transition(current_state, device_actions)
            
            #  修改7：每个设备独立计算 reward
            device_rewards = {}
            for dev in self.simulator.device_names:
                device_rewards[dev] = self.compute_device_reward(
                    dev, current_state, next_state, ideal_state, device_effects[dev]
                )
            
            n_emo = self.simulator.n_emotions
            distance = np.linalg.norm(
                self.normalize_state(next_state)[:n_emo+1] - 
                self.normalize_state(ideal_state)[:n_emo+1]
            )
            terminated = (distance < self.success_threshold)
            truncated = (step == max_steps - 1)
            done = terminated or truncated
            
            trajectory.append({
                'state': current_state,
                'ideal_state': ideal_state,
                'device_actions': device_actions,
                'device_log_probs': device_log_probs,
                'device_rewards': device_rewards,
                'device_values': {dev: float(device_values[dev].item()) for dev in self.simulator.device_names},
                'next_state': next_state,
                'done': done,
                'terminated': terminated
            })
            
            current_state = next_state
            if done:
                break
        
        return trajectory
    
    def compute_device_gae(self, trajectory, dev):
        """ 修改8：每个设备独立计算 GAE"""
        rewards = [t['device_rewards'][dev] for t in trajectory]
        values = [t['device_values'][dev] for t in trajectory]
        
        advantages = []
        returns = []
        
        T = len(trajectory)
        gae = 0.0
        
        for t in reversed(range(T)):
            if trajectory[t].get('terminated', False):
                next_value = 0.0
            else:
                next_value = trajectory[t + 1]['device_values'][dev] if t < T - 1 else 0.0
            
            td_error = rewards[t] + self.gamma * next_value - values[t]
            gae = td_error + self.gamma * self.gae_lambda * gae
            advantages.insert(0, gae)
            returns.insert(0, gae + values[t])
        
        return advantages, returns
    
    def update_policy(self, trajectories, epochs=4):
        """ 修改9：每个设备独立更新策略"""
        
        # 按设备组织数据
        device_data = {dev: {
            'states': [],
            'ideal_states': [],
            'actions': [],
            'old_log_probs': [],
            'advantages': [],
            'returns': []
        } for dev in self.simulator.device_names}
        
        for traj in trajectories:
            for dev in self.simulator.device_names:
                advantages, returns = self.compute_device_gae(traj, dev)
                
                for i, trans in enumerate(traj):
                    device_data[dev]['states'].append(self.normalize_state(trans['state']))
                    device_data[dev]['ideal_states'].append(self.normalize_state(trans['ideal_state']))
                    device_data[dev]['actions'].append(trans['device_actions'][dev])
                    device_data[dev]['old_log_probs'].append(trans['device_log_probs'][dev])
                    device_data[dev]['advantages'].append(advantages[i])
                    device_data[dev]['returns'].append(returns[i])
        
        # 转换为 tensor
        for dev in self.simulator.device_names:
            device_data[dev]['states'] = torch.FloatTensor(device_data[dev]['states']).to(self.device)
            device_data[dev]['ideal_states'] = torch.FloatTensor(device_data[dev]['ideal_states']).to(self.device)
            device_data[dev]['actions'] = torch.LongTensor(device_data[dev]['actions']).to(self.device)
            device_data[dev]['old_log_probs'] = torch.stack([
                p.to(self.device) if isinstance(p, torch.Tensor) else torch.tensor(p, device=self.device)
                for p in device_data[dev]['old_log_probs']
            ]).squeeze()
            device_data[dev]['advantages'] = torch.FloatTensor(device_data[dev]['advantages']).to(self.device)
            device_data[dev]['returns'] = torch.FloatTensor(device_data[dev]['returns']).to(self.device)
            
            # 标准化
            device_data[dev]['advantages'] = (
                device_data[dev]['advantages'] - device_data[dev]['advantages'].mean()
            ) / (device_data[dev]['advantages'].std() + 1e-8)
        
        for epoch in range(epochs):
            # 一次 forward pass 获取所有设备的输出
            all_states = device_data[self.simulator.device_names[0]]['states']
            all_ideal_states = device_data[self.simulator.device_names[0]]['ideal_states']
            
            device_probs, device_values = self.policy(all_states, all_ideal_states)
            
            total_loss = 0.0
            
            #  每个设备独立计算损失
            for dev in self.simulator.device_names:
                probs = device_probs[dev]
                values = device_values[dev]
                actions = device_data[dev]['actions']
                old_log_probs = device_data[dev]['old_log_probs']
                advantages = device_data[dev]['advantages']
                returns = device_data[dev]['returns']
                
                dist = Categorical(probs)
                new_log_probs = dist.log_prob(actions)
                entropy = dist.entropy().mean()
                
                ratio = torch.exp(new_log_probs - old_log_probs)
                surr1 = ratio * advantages
                surr2 = torch.clamp(ratio, 1 - self.clip_epsilon, 1 + self.clip_epsilon) * advantages
                policy_loss = -torch.min(surr1, surr2).mean()
                
                value_loss = F.mse_loss(values, returns)
                
                device_loss = policy_loss + self.value_coef * value_loss - self.entropy_coef * entropy
                total_loss += device_loss
                
                if epoch == epochs - 1:
                    self.device_policy_losses[dev].append(policy_loss.item())
                    self.device_value_losses[dev].append(value_loss.item())
            
            self.optimizer.zero_grad()
            total_loss.backward()
            torch.nn.utils.clip_grad_norm_(self.policy.parameters(), 0.5)
            self.optimizer.step()
    
    def train(self, dataset, episodes=2000, batch_size=16):
        print("\n" + "="*70)
        print(" 修复版：设备独立决策（无竞争）")
        print("="*70 + "\n")
        
        self.policy.train()
        
        for episode in range(episodes):
            trajectories = []
            episode_total_reward = 0
            
            for _ in range(batch_size):
                sample = dataset[np.random.randint(len(dataset))]
                trajectory = self.collect_trajectory(sample['initial_state'], sample['ideal_state'])
                trajectories.append(trajectory)
                
                # 计算总奖励（所有设备之和）
                traj_reward = sum([sum(t['device_rewards'].values()) for t in trajectory])
                episode_total_reward += traj_reward
            
            self.update_policy(trajectories, epochs=4)
            
            avg_reward = episode_total_reward / batch_size
            self.episode_rewards.append(avg_reward)
            
            if (episode + 1) % 100 == 0:
                recent_rewards = self.episode_rewards[-100:]
                avg_recent = np.mean(recent_rewards)
                print(f"Episode {episode+1}/{episodes} | Avg Reward: {avg_recent:.2f}")
            
            if (episode + 1) % 500 == 0:
                self.save_model()
        
        print("\n 训练完成!\n")
    
    def save_model(self, path='logs/checkpoints/UserState_fixed.pth'):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        torch.save({
            'policy': self.policy.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'episode_rewards': self.episode_rewards
        }, path)
        print(f" 模型已保存: {path}")
    
    def load_model(self, path='logs/checkpoints/UserState_fixed.pth'):
        try:
            checkpoint = torch.load(path, map_location=self.device, weights_only=False)
            self.policy.load_state_dict(checkpoint['policy'])
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.episode_rewards = checkpoint.get('episode_rewards', [])
            print(f" 模型已加载: {path}")
        except Exception as e:
            print(f" 加载失败: {e}")
    
    def evaluate(self, dataset, n_samples=500):
        self.policy.eval()
        successes = 0
        steps_list = []
        device_action_counts = {
            dev: np.zeros(len(self.simulator.device_actions[dev]))
            for dev in self.simulator.device_names
        }
        
        n_emo = self.simulator.n_emotions
        
        with torch.no_grad():
            for i in range(n_samples):
                sample = dataset[np.random.randint(len(dataset))]
                current_state = sample['initial_state'].copy()
                ideal_state = sample['ideal_state']
                
                self.simulator.reset_device_states()
                success = False
                
                for step in range(self.max_steps):
                    current_norm = torch.FloatTensor(self.normalize_state(current_state)).unsqueeze(0).to(self.device)
                    ideal_norm = torch.FloatTensor(self.normalize_state(ideal_state)).unsqueeze(0).to(self.device)
                    
                    device_probs, _ = self.policy(current_norm, ideal_norm)
                    
                    device_actions = {}
                    for dev in self.simulator.device_names:
                        action = Categorical(device_probs[dev]).sample().item()
                        device_actions[dev] = action
                        device_action_counts[dev][action] += 1
                    
                    next_state, _ = self.simulator.state_transition(current_state, device_actions)
                    
                    dist = np.linalg.norm(
                        self.normalize_state(next_state)[:n_emo+1] - 
                        self.normalize_state(ideal_state)[:n_emo+1]
                    )
                    
                    if dist < self.success_threshold:
                        successes += 1
                        steps_list.append(step + 1)
                        success = True
                        break
                    
                    current_state = next_state
                
                if not success:
                    steps_list.append(self.max_steps)
        
        accuracy = successes / n_samples
        
        print("\n" + "="*70)
        print("评估结果")
        print("="*70)
        print(f"准确率: {accuracy*100:.2f}% ({successes}/{n_samples})")
        print(f"平均步数: {np.mean(steps_list):.2f}")
        
        print("\n各设备动作分布:")
        for dev in self.simulator.device_names:
            print(f"\n【{dev.upper()}】")
            total = device_action_counts[dev].sum()
            for i, action_name in enumerate(self.simulator.device_actions[dev]):
                count = device_action_counts[dev][i]
                pct = (count / total * 100) if total > 0 else 0
                print(f"  {action_name:12s}: {int(count):5d} ({pct:5.1f}%)")
        print("="*70 + "\n")


# ============= 主函数 =============
def train():
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    print(f'使用设备: {device}\n')
    
    simulator = UserStateSimulator()
    
    data_path = 'data/UserState_fixed_train.pkl'
    if not os.path.exists(data_path):
        dataset = simulator.generate_dataset(n_samples=10000, save_path=data_path)
    else:
        print(f'加载数据: {data_path}')
        with open(data_path, 'rb') as f:
            dataset = pickle.load(f)
        print(f' 已加载 {len(dataset)} 条\n')
    
    trainer = FactorizedPPOTrainer(simulator, device=device)
    
    if os.path.exists('logs/checkpoints/UserState_fixed.pth'):
        trainer.load_model()
    
    trainer.train(dataset, episodes=2000, batch_size=16)
    trainer.save_model()
    
    print("\n最终评估...")
    eval()
    # trainer.evaluate(dataset, n_samples=500)


def eval():
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    simulator = UserStateSimulator()
    trainer = FactorizedPPOTrainer(simulator, device=device)
    
    checkpoint_path = 'logs/checkpoints/UserState_fixed.pth'
    if not os.path.exists(checkpoint_path):
        print(f" 未找到模型: {checkpoint_path}")
        return
    
    trainer.load_model(checkpoint_path)
    
    val_path = 'data/UserState_fixed_val.pkl'
    if not os.path.exists(val_path):
        dataset = simulator.generate_dataset(n_samples=1000, save_path=val_path)
    else:
        with open(val_path, 'rb') as f:
            dataset = pickle.load(f)
    
    trainer.evaluate(dataset, n_samples=1000)


def inference():
    test_scenarios = [
        {
            'name': '高度焦虑',
            'state': [0.1, 0.4, 0.1, 0.1, 0.2, 0.05, 0.03, 0.02, 65.0]
        },
        {
            'name': '过度悲伤',
            'state': [0.05, 0.05, 0.02, 0.03, 0.05, 0.02, 0.7, 0.08, 40.0]
        }
    ]
    
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    simulator = UserStateSimulator()
    trainer = FactorizedPPOTrainer(simulator, device=device)
    
    checkpoint_path = 'logs/checkpoints/UserState_fixed.pth'
    if not os.path.exists(checkpoint_path):
        print(f" 未找到模型: {checkpoint_path}")
        return
    
    trainer.load_model(checkpoint_path)
    trainer.policy.eval()
    
    for scenario in test_scenarios:
        base_state = np.array(scenario['state'], dtype=np.float32)
        simulator.reset_device_states()
        device_encoding = simulator.encode_device_states()
        current_state = np.concatenate([base_state, device_encoding])
        
        ideal_state = simulator.compute_ideal_state(current_state)
        
        current_norm = torch.FloatTensor(trainer.normalize_state(current_state)).unsqueeze(0).to(device)
        ideal_norm = torch.FloatTensor(trainer.normalize_state(ideal_state)).unsqueeze(0).to(device)
        
        with torch.no_grad():
            device_probs, device_values = trainer.policy(current_norm, ideal_norm)
            
            print(f"\n[{scenario['name']}]")
            for dev in simulator.device_names:
                action = Categorical(device_probs[dev]).sample().item()
                action_name = simulator.device_actions[dev][action]
                value = device_values[dev].item()
                print(f"  {dev:<8} → {action_name:15s} (V={value:+.2f})")


if __name__ == '__main__':
    print("\n" + "="*70)
    print(" 核心修复点")
    print("="*70)
    print("1. 每个设备独立的 reward 分量（不再共享全局 reward）")
    print("2. 成本/频率惩罚归属到设备自己")
    print("3. 关闭动作有机会成本（距离目标远时）")
    print("4. 每个设备独立的 Critic（估计自己的价值）")
    print("5. 每个设备独立计算 GAE 和更新策略")
    print("="*70 + "\n")
    
    if len(sys.argv) > 1:
        if sys.argv[1] == 'inference':
            inference()
        elif sys.argv[1] == 'eval':
            eval()
        else:
            train()
    else:
        train()