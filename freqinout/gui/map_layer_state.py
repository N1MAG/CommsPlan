"""Pure compatibility policy for the operator map's visible layers."""
EXCLUSIVE_LAYERS = frozenset({'regional', 'propagation', 'pins', 'mesh_nodes', 'peer'})
LAYERS = EXCLUSIVE_LAYERS | {'stations', 'paths', 'traffic'}


def toggle_map_layer(current, layer: str, checked: bool) -> frozenset[str]:
    selected = set(current) & LAYERS
    if layer not in LAYERS:
        return frozenset(selected)
    if checked:
        if layer == 'paths' and 'mesh_nodes' in selected:
            return frozenset({'mesh_nodes', 'paths'})
        if layer in EXCLUSIVE_LAYERS:
            selected = {layer}
            if layer in {'propagation', 'peer'}:
                selected.add('stations')
            if layer == 'propagation':
                selected.add('paths')
        else:
            selected.difference_update(EXCLUSIVE_LAYERS)
            selected.add(layer)
        if 'paths' in selected:
            selected.add('stations')
    else:
        selected.discard(layer)
        if layer == 'stations':
            selected.difference_update({'paths', 'propagation', 'peer'})
        elif layer == 'paths':
            selected.discard('propagation')
        elif layer == 'propagation':
            selected.difference_update({'stations', 'paths'})
        elif layer == 'mesh_nodes':
            selected.discard('paths')
        elif layer == 'peer':
            selected.discard('stations')
    return frozenset(selected)
