"""Small synthetic forward/backward test; runs on CPU or CUDA."""

import torch

from hydroctgssm import HydroCTGSSM, censored_gaussian_nll


def main() -> None:
    torch.manual_seed(7)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    n, length, k, dynamic_dim, static_dim, edge_dim = 12, 8, 4, 3, 6, 4
    model = HydroCTGSSM(k, dynamic_dim, static_dim, edge_dim, hidden_dim=32, graph_layers=2).to(device)
    values = torch.randn(n, length, k, device=device)
    observed = (torch.rand(n, length, k, device=device) > 0.45).float()
    censor = torch.zeros_like(values)
    censor[torch.rand_like(values) < 0.08] = -1
    delta = torch.randint(1, 100, (n, length), device=device).float()
    dynamic = torch.randn(n, length, dynamic_dim, device=device)
    static = torch.randn(n, static_dim, device=device)
    valid = torch.ones(n, length, device=device)
    edge_index = torch.tensor([[0, 1, 2, 3, 5, 7], [1, 2, 3, 4, 6, 8]], device=device)
    edge_attr = torch.randn(edge_index.size(1), edge_dim, device=device)
    out = model(values, observed, censor, delta, dynamic, static, valid, edge_index, edge_attr)
    target = torch.randn(n, k, device=device)
    target_obs = (torch.rand(n, k, device=device) > 0.2).float()
    target_censor = torch.zeros_like(target)
    target_censor[0, 0] = -1
    loss = censored_gaussian_nll(out["location"], out["scale"], target, target_obs, target_censor)
    loss.backward()
    assert torch.isfinite(loss)
    assert all(p.grad is None or torch.isfinite(p.grad).all() for p in model.parameters())
    print({"device": device, "loss": float(loss), "output_shape": list(out["location"].shape)})


if __name__ == "__main__":
    main()

