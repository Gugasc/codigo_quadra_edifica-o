from qgis.core import (
    QgsProject,
    QgsFeature,
    QgsFeatureRequest,
    QgsSpatialIndex,
    QgsGeometry,
    Qgis,
    QgsAggregateCalculator,
    QgsExpression,
    QgsWkbTypes
)
from qgis.utils import iface
from qgis.PyQt.QtWidgets import QInputDialog

# ==========================================
# 1. NOMES DAS CAMADAS NO PROJETO DO QGIS
# ==========================================
NOME_CAMADA_EDIF = 'ct_edificacao_fiscal'
NOME_CAMADA_LOTES = 'ct_lote_fiscal'
NOME_CAMADA_QUADRAS = 'ct_quadra_fiscal'
NOME_CAMADA_SETORES = 'ct_setor_fiscal'

NOME_CAMPO_SETOR = 'cod_sf'

def obter_camada_do_projeto(nome_camada):
    camadas = QgsProject.instance().mapLayersByName(nome_camada)
    return camadas[0] if camadas else None

def pegar_maior_qf_do_setor(layer, valor_sf_sat):
    if layer.fields().indexOf('cod_sf_sat') == -1 or layer.fields().indexOf('cod_qf') == -1:
        return 0
    expressao = f'"cod_sf_sat" = \'{valor_sf_sat}\' OR "cod_sf_sat" = {valor_sf_sat}'
    parametros = QgsAggregateCalculator.AggregateParameters()
    parametros.filter = expressao
    max_val, ok = layer.aggregate(QgsAggregateCalculator.Max, 'cod_qf', parametros)
    if ok and max_val is not None:
        return int(max_val)
    return 0

def pegar_maior_valor_geral(layer, nome_campo):
    if layer.fields().indexOf(nome_campo) == -1:
        return 0
    max_val, ok = layer.aggregate(QgsAggregateCalculator.Max, nome_campo)
    if ok and max_val is not None:
        return int(max_val)
    return 0

def extrair_lotes_e_quadras_por_setor():
    project = QgsProject.instance()
    iface.messageBar().pushMessage("Aguarde", "Verificando camadas no projeto...", level=Qgis.Info, duration=2)
    
    layer_edif = obter_camada_do_projeto(NOME_CAMADA_EDIF)
    layer_lotes = obter_camada_do_projeto(NOME_CAMADA_LOTES)
    layer_quadras = obter_camada_do_projeto(NOME_CAMADA_QUADRAS)
    layer_setores = obter_camada_do_projeto(NOME_CAMADA_SETORES)
    
    if not all([layer_edif, layer_lotes, layer_quadras, layer_setores]):
        iface.messageBar().pushMessage("Erro", "Não foi possível encontrar todas as camadas.", level=Qgis.Critical, duration=7)
        return

    numero_digitado, ok = QInputDialog.getText(None, "Filtrar Setor", "Digite o código do setor (cod_sf):")
    if not ok or not numero_digitado.strip():
        return
        
    expressao = f'"{NOME_CAMPO_SETOR}" = {numero_digitado}'
    request_setor = QgsFeatureRequest().setFilterExpression(expressao)
    setores = list(layer_setores.getFeatures(request_setor))
    
    if not setores:
        iface.messageBar().pushMessage("Erro", "Nenhum setor encontrado.", level=Qgis.Critical, duration=5)
        return

    setor_selecionado = setores[0]
    valor_sf_sat = setor_selecionado['cod_sf_sat']
    geom_setor = setor_selecionado.geometry()
    bbox_setor = geom_setor.boundingBox()

    iface.messageBar().pushMessage("Aguarde", "Calculando contadores e identificadores...", level=Qgis.Info, duration=3)
    maior_qf = max(
        pegar_maior_qf_do_setor(layer_quadras, valor_sf_sat),
        pegar_maior_qf_do_setor(layer_lotes, valor_sf_sat)
    )
    proximo_qf = maior_qf + 1

    # =========================================================================
    # OTIMIZAÇÃO 1: Pré-fetch de Geometrias na memória sem trazer atributos
    # =========================================================================
    iface.messageBar().pushMessage("Aguarde", "Criando índices espaciais...", level=Qgis.Info, duration=3)
    req_geom_only = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([])

    geoms_lotes = {}
    index_lotes = QgsSpatialIndex()
    for feat in layer_lotes.getFeatures(req_geom_only):
        geoms_lotes[feat.id()] = feat.geometry()
        index_lotes.addFeature(feat)

    geoms_quadras = {}
    index_quadras = QgsSpatialIndex()
    for feat in layer_quadras.getFeatures(req_geom_only):
        geoms_quadras[feat.id()] = feat.geometry()
        index_quadras.addFeature(feat)

    idx_e_sql = layer_edif.fields().indexOf('sql')
    idx_e_sqle = layer_edif.fields().indexOf('sqle')
    idx_e_ef = layer_edif.fields().indexOf('cod_ef')
    req_edif = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_e_sql, idx_e_sqle, idx_e_ef])
    
    edificacoes_in_bbox = {}
    index_edif = QgsSpatialIndex()
    for feat in layer_edif.getFeatures(req_edif):
        edificacoes_in_bbox[feat.id()] = feat
        index_edif.addFeature(feat)

    idx_q_sq, idx_q_sat, idx_q_qf, idx_q_sf, idx_q_id, idx_q_fid = [layer_quadras.fields().indexOf(f) for f in ['sq', 'cod_sf_sat', 'cod_qf', 'cod_sf', 'id', 'fid']]
    idx_l_sq, idx_l_sql, idx_l_sat, idx_l_qf, idx_l_lf, idx_l_id, idx_l_fid = [layer_lotes.fields().indexOf(f) for f in ['sq', 'sql', 'cod_sf_sat', 'cod_qf', 'cod_lf', 'id', 'fid']]

    ids_edificacoes_no_setor = index_edif.intersects(bbox_setor)
    edificacoes_isoladas = {}
    
    # =========================================================================
    # OTIMIZAÇÃO 2: Verificações 100% na memória Ram
    # =========================================================================
    for id_edif in ids_edificacoes_no_setor:
        feat_edif = edificacoes_in_bbox[id_edif]
        geom_edif = feat_edif.geometry()
        if not geom_edif.within(geom_setor):
            continue
        bbox_edif = geom_edif.boundingBox()

        toca_lote = any(geom_edif.intersects(geoms_lotes[id_l]) for id_l in index_lotes.intersects(bbox_edif))
        if toca_lote: continue
        toca_quadra = any(geom_edif.intersects(geoms_quadras[id_q]) for id_q in index_quadras.intersects(bbox_edif))
        if toca_quadra: continue

        edificacoes_isoladas[id_edif] = feat_edif

    # 8. Agrupa as edificações que se tocam
    index_isoladas = QgsSpatialIndex()
    for feat in edificacoes_isoladas.values():
        index_isoladas.addFeature(feat)

    grupos = []
    visitados = set()
    for id_feat, feat in edificacoes_isoladas.items():
        if id_feat in visitados: continue
        grupo_atual = [feat]
        visitados.add(id_feat)
        fila = [feat]
        while fila:
            atual = fila.pop(0)
            geom_atual = atual.geometry()
            vizinhos_ids = index_isoladas.intersects(geom_atual.boundingBox())
            for v_id in vizinhos_ids:
                if v_id not in visitados:
                    outra_feat = edificacoes_isoladas[v_id]
                    if geom_atual.intersects(outra_feat.geometry()):
                        visitados.add(v_id)
                        grupo_atual.append(outra_feat)
                        fila.append(outra_feat)
        grupos.append(grupo_atual)

    # =========================================================================
    # 9. Criação de Quadras, Lotes e Mapeamento de Atualizações
    # =========================================================================
    str_sf_sat = str(valor_sf_sat).zfill(3)
    novas_features_lote = []
    novas_features_quadra = []
    mapa_edificacoes_para_atualizar = {}

    wkb_quadras = layer_quadras.wkbType()
    wkb_lotes = layer_lotes.wkbType()

    for grupo in grupos:
        geom_combinada = QgsGeometry(grupo[0].geometry())
        for feat in grupo[1:]:
            geom_combinada = geom_combinada.combine(feat.geometry())

        is_multi_quadra = QgsWkbTypes.isMultiType(wkb_quadras)
        
        if is_multi_quadra and not geom_combinada.isMultipart():
            geom_combinada.convertToMultiType()
        elif not is_multi_quadra and geom_combinada.isMultipart():
            maior_area = -1
            geom_simples = geom_combinada
            for part in geom_combinada.asGeometryCollection():
                if part.area() > maior_area:
                    maior_area = part.area()
                    geom_simples = part
            geom_combinada = geom_simples
        
        str_qf_atual = str(proximo_qf).zfill(4)
        
        # Gera o "sq" (ex: 201 + 0134 = 2010134) -> 7 caracteres
        str_sq_atual = f"{str_sf_sat}{str_qf_atual}"

        nova_feat_quadra = QgsFeature(layer_quadras.fields())
        nova_feat_quadra.setGeometry(geom_combinada)
        
        if idx_q_sat != -1: nova_feat_quadra[idx_q_sat] = valor_sf_sat
        if idx_q_qf != -1: nova_feat_quadra[idx_q_qf] = proximo_qf
        if idx_q_sf != -1: nova_feat_quadra[idx_q_sf] = int(numero_digitado)
        
        novas_features_quadra.append(nova_feat_quadra)

        cod_lf_atual = 1
        for feat_edif in grupo:
            
            # Formata o número do lote com zeros à esquerda (ex: '001') -> 3 caracteres
            str_lf_atual = str(cod_lf_atual).zfill(3)
            
            # ====================================================================
            # CONCATENAÇÃO ATIVADA: sq (7) + cod_lf (3) = sql (10 caracteres)
            # ====================================================================
            str_sql_atual = f"{str_sq_atual}{str_lf_atual}"
            str_sqle_atual = f"{str_sql_atual}01"

            atributos_novos = {}
            
            # Grava os identificadores gerados na Edificação
            if idx_e_sql != -1: atributos_novos[idx_e_sql] = str_sql_atual
            if idx_e_sqle != -1: atributos_novos[idx_e_sqle] = str_sqle_atual
            
            mapa_edificacoes_para_atualizar[feat_edif.id()] = atributos_novos

            geom_lote = QgsGeometry(feat_edif.geometry())
            is_multi_lote = QgsWkbTypes.isMultiType(wkb_lotes)

            if is_multi_lote and not geom_lote.isMultipart():
                geom_lote.convertToMultiType()
            elif not is_multi_lote and geom_lote.isMultipart():
                maior_area_l = -1
                geom_simples_l = geom_lote
                for part in geom_lote.asGeometryCollection():
                    if part.area() > maior_area_l:
                        maior_area_l = part.area()
                        geom_simples_l = part
                geom_lote = geom_simples_l

            nova_feat_lote = QgsFeature(layer_lotes.fields())
            nova_feat_lote.setGeometry(geom_lote)
            
            # ---> SALVANDO O SQL NO LOTE <---
            if idx_l_sql != -1: nova_feat_lote[idx_l_sql] = str_sql_atual
            if idx_l_sat != -1: nova_feat_lote[idx_l_sat] = valor_sf_sat
            if idx_l_qf != -1: nova_feat_lote[idx_l_qf] = proximo_qf
            if idx_l_lf != -1: nova_feat_lote[idx_l_lf] = cod_lf_atual
            
            novas_features_lote.append(nova_feat_lote)

            cod_lf_atual += 1

        proximo_qf += 1

    # =========================================================================
    # 10. Inicia a edição na memória
    # =========================================================================
    if novas_features_lote or novas_features_quadra or mapa_edificacoes_para_atualizar:
        if novas_features_lote:
            layer_lotes.startEditing()
            layer_lotes.addFeatures(novas_features_lote)
            layer_lotes.triggerRepaint()

        if novas_features_quadra:
            layer_quadras.startEditing()
            layer_quadras.addFeatures(novas_features_quadra)
            layer_quadras.triggerRepaint()

        if mapa_edificacoes_para_atualizar:
            layer_edif.startEditing()
            for fid, atributos in mapa_edificacoes_para_atualizar.items():
                for idx_campo, novo_valor in atributos.items():
                    layer_edif.changeAttributeValue(fid, idx_campo, novo_valor)
            layer_edif.triggerRepaint()

        iface.messageBar().pushMessage(
            "Sucesso",
            f"Processado: {len(novas_features_quadra)} Quadras, {len(novas_features_lote)} Lotes e {len(mapa_edificacoes_para_atualizar)} Edificações atualizadas!",
            level=Qgis.Success,
            duration=7
        )
    else:
        iface.messageBar().pushMessage("Concluído", "Nenhuma edificação isolada precisou ser alterada.", level=Qgis.Info, duration=5)

extrair_lotes_e_quadras_por_setor()