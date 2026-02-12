import numpy as np
def cosine_similarity(a, b) -> float:
    a = np.array(a, dtype=np.float64); b = np.array(b, dtype=np.float64)
    na = np.linalg.norm(a); nb = np.linalg.norm(b)
    if na == 0.0 or nb == 0.0: return 0.0
    return float(np.dot(a,b)/(na*nb))
