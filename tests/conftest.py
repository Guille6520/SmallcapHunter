import sys
from pathlib import Path

# Los módulos del proyecto (detective.py, embeddings.py, etc.) viven en
# la raíz del repo, no en un paquete instalado — los añado al path para
# poder importarlos igual que lo hace cualquier script del pipeline.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
