from qgis.core import (
    QgsProject,
    QgsFeature,
    QgsFeatureRequest,
    QgsSpatialIndex,
    QgsGeometry,
    Qgis,
    QgsWkbTypes,
    NULL
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

def extrair_lotes_por_quadra_existente():
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
    print(f"Número do Setor: {numero_digitado}")    
    expressao = f'"{NOME_CAMPO_SETOR}" = {numero_digitado}'
    request_setor = QgsFeatureRequest().setFilterExpression(expressao)
    setores = list(layer_setores.getFeatures(request_setor))
    
    if not setores:
        iface.messageBar().pushMessage("Erro", "Nenhum setor encontrado.", level=Qgis.Critical, duration=5)
        return

    setor_selecionado = setores[0]
    geom_setor = setor_selecionado.geometry()
    bbox_setor = geom_setor.boundingBox()

    iface.messageBar().pushMessage("Aguarde", "Criando índices espaciais...", level=Qgis.Info, duration=3)

    # Pegando os índices dos campos
    idx_q_sq, idx_q_sat, idx_q_qf = [layer_quadras.fields().indexOf(f) for f in ['sq', 'cod_sf_sat', 'cod_qf']]
    idx_l_sq, idx_l_sql, idx_l_sat, idx_l_qf, idx_l_lf = [layer_lotes.fields().indexOf(f) for f in ['sq', 'sql', 'cod_sf_sat', 'cod_qf', 'cod_lf']]
    
    # Mantemos o índice do sqle aqui apenas para verificar se ele já está preenchido
    idx_e_sql, idx_e_sqle = [layer_edif.fields().indexOf(f) for f in ['sql', 'sqle']]

    # Cache de Quadras
    req_quadras = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_q_sq, idx_q_sat, idx_q_qf])
    quadras_in_bbox = {}
    index_quadras = QgsSpatialIndex()
    for feat in layer_quadras.getFeatures(req_quadras):
        quadras_in_bbox[feat.id()] = feat
        index_quadras.addFeature(feat)

    # Cache de Lotes Existentes
    req_lotes = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_l_sq, idx_l_lf])
    lotes_in_bbox = {}
    index_lotes = QgsSpatialIndex()
    
    max_lf_dict = {}
    for feat in layer_lotes.getFeatures(req_lotes):
        lotes_in_bbox[feat.id()] = feat
        index_lotes.addFeature(feat)
        
        # Mapeia qual é o maior lote já existente dentro de cada quadra
        sq_val = feat.attribute(idx_l_sq)
        lf_val = feat.attribute(idx_l_lf)
        if sq_val not in (None, NULL) and lf_val not in (None, NULL):
            try:
                lf_int = int(lf_val)
                if sq_val not in max_lf_dict or lf_int > max_lf_dict[sq_val]:
                    max_lf_dict[sq_val] = lf_int
            except ValueError:
                pass

    # Cache de Edificações
    req_edif = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_e_sql, idx_e_sqle])
    edificacoes_in_bbox = {}
    index_edif = QgsSpatialIndex()
    for feat in layer_edif.getFeatures(req_edif):
        edificacoes_in_bbox[feat.id()] = feat
        index_edif.addFeature(feat)

    ids_edificacoes_no_setor = index_edif.intersects(bbox_setor)
    
    edificacoes_por_quadra = {}
    
    # =========================================================================
    # Regras de Negócio Espaciais
    # =========================================================================
    for id_edif in ids_edificacoes_no_setor:
        feat_edif = edificacoes_in_bbox[id_edif]
        
        # 1. Pula a edificação se ela já possuir SQL ou SQLE anotado
        val_sql = feat_edif.attribute(idx_e_sql)
        val_sqle = feat_edif.attribute(idx_e_sqle)
        
        sql_preenchido = val_sql not in (None, NULL) and str(val_sql).strip() != ''
        sqle_preenchido = val_sqle not in (None, NULL) and str(val_sqle).strip() != ''
        
        if sql_preenchido or sqle_preenchido:
            continue
            
        geom_edif = feat_edif.geometry()
        
        if not geom_edif.within(geom_setor):
            continue
            
        bbox_edif = geom_edif.boundingBox()

        # 2. O local não pode ter um Lote já desenhado
        toca_lote = any(geom_edif.intersects(lotes_in_bbox[id_l].geometry()) for id_l in index_lotes.intersects(bbox_edif))
        if toca_lote: 
            continue

        # 3. O local deve estar 100% dentro de exatamente UMA Quadra
        quadras_contendo = []
        for id_q in index_quadras.intersects(bbox_edif):
            if geom_edif.within(quadras_in_bbox[id_q].geometry()):
                quadras_contendo.append(quadras_in_bbox[id_q])
        
        if len(quadras_contendo) != 1:
            continue 

        quadra_valida = quadras_contendo[0]
        q_id = quadra_valida.id()
        if q_id not in edificacoes_por_quadra:
            edificacoes_por_quadra[q_id] = []
        edificacoes_por_quadra[q_id].append(feat_edif)

    # =========================================================================
    # Criação de Lotes e Mapeamento de Atualizações
    # =========================================================================
    novas_features_lote = []
    mapa_edificacoes_para_atualizar = {}

    wkb_lotes = layer_lotes.wkbType()
    is_multi_lote = QgsWkbTypes.isMultiType(wkb_lotes)

    for q_id, lista_edif in edificacoes_por_quadra.items():
        quadra = quadras_in_bbox[q_id]
        
        str_sq = str(quadra.attribute(idx_q_sq)) if quadra.attribute(idx_q_sq) not in (None, NULL) else ""
        cod_qf = quadra.attribute(idx_q_qf)
        valor_sf_sat = quadra.attribute(idx_q_sat)
        
        if not str_sq:
            continue
            
        cod_lf_atual = max_lf_dict.get(str_sq, 0) + 1

        for feat_edif in lista_edif:

            # Gera as strings de código (Garante SQL com 10 caracteres)
            str_lf_atual = str(cod_lf_atual).zfill(3)
            str_sql_atual = f"{str_sq}{str_lf_atual}"
            
            # REMOVIDA A CRIAÇÃO DO SQLE (str_sqle_atual) AQUI

            atributos_novos = {}
            if idx_e_sql != -1: atributos_novos[idx_e_sql] = str_sql_atual
            
            # REMOVIDA A ATRIBUIÇÃO DO SQLE NO DICIONÁRIO AQUI
            
            mapa_edificacoes_para_atualizar[feat_edif.id()] = atributos_novos

            # Clonagem de geometria, evitando erro topológico no PostGIS
            geom_lote = QgsGeometry(feat_edif.geometry())

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

            # Prepara a feição do novo Lote
            nova_feat_lote = QgsFeature(layer_lotes.fields())
            nova_feat_lote.setGeometry(geom_lote)
            
            # Salvando os atributos exigidos pelo QGIS/Python
            if idx_l_sql != -1: nova_feat_lote[idx_l_sql] = str_sql_atual
            if idx_l_sat != -1: nova_feat_lote[idx_l_sat] = valor_sf_sat
            if idx_l_qf != -1: nova_feat_lote[idx_l_qf] = cod_qf
            if idx_l_lf != -1: nova_feat_lote[idx_l_lf] = cod_lf_atual
            
            novas_features_lote.append(nova_feat_lote)

            cod_lf_atual += 1

    # =========================================================================
    # Inicia a edição nas camadas
    # =========================================================================
    if novas_features_lote or mapa_edificacoes_para_atualizar:
        if novas_features_lote:
            layer_lotes.startEditing()
            layer_lotes.addFeatures(novas_features_lote)
            layer_lotes.triggerRepaint()

        if mapa_edificacoes_para_atualizar:
            layer_edif.startEditing()
            for fid, atributos in mapa_edificacoes_para_atualizar.items():
                for idx_campo, novo_valor in atributos.items():
                    layer_edif.changeAttributeValue(fid, idx_campo, novo_valor)
            layer_edif.triggerRepaint()

        iface.messageBar().pushMessage(
            "Sucesso",
            f"Processado: {len(novas_features_lote)} Lotes criados e {len(mapa_edificacoes_para_atualizar)} Edificações atualizadas!",
            level=Qgis.Success,
            duration=7
        )
    else:
        iface.messageBar().pushMessage("Concluído", "Nenhuma edificação atendeu aos critérios para criar lote.", level=Qgis.Info, duration=5)

extrair_lotes_por_quadra_existente()