from qgis.core import (
    QgsProject, 
    QgsFeature, 
    QgsFeatureRequest,
    QgsSpatialIndex,
    QgsGeometry,
    Qgis,
    QgsAggregateCalculator,
    QgsExpression
)
from qgis.utils import iface
from qgis.PyQt.QtWidgets import QInputDialog

# ==========================================
# 1. NOMES DAS CAMADAS NO PROJETO DO QGIS
# ==========================================
NOME_CAMADA_EDIF = 'edificacao_fiscal — ct_edificacao_fiscal'
NOME_CAMADA_LOTES = 'lote_fiscal — ct_lote_fiscal'
NOME_CAMADA_QUADRAS = 'quadra_fiscal — ct_quadra_fiscal'
NOME_CAMADA_SETORES = 'ct_setor_fiscal'

NOME_CAMPO_SETOR = 'cod_sf' 

def obter_camada_do_projeto(nome_camada):
    camadas = QgsProject.instance().mapLayersByName(nome_camada)
    return camadas[0] if camadas else None

def pegar_maior_qf_do_setor(layer, valor_sf_sat):
    """ Busca o maior cod_qf na camada usando agregador nativo C++ (Muito mais rápido) """
    if layer.fields().indexOf('cod_sf_sat') == -1 or layer.fields().indexOf('cod_qf') == -1:
        return 0
    
    expressao = f'"cod_sf_sat" = \'{valor_sf_sat}\' OR "cod_sf_sat" = {valor_sf_sat}'
    
    # Configura os parâmetros de agregação adicionando o filtro
    parametros = QgsAggregateCalculator.AggregateParameters()
    parametros.filter = expressao
    
    # Executa o agregador com os parâmetros corretos
    max_val, ok = layer.aggregate(QgsAggregateCalculator.Max, 'cod_qf', parametros)
    
    if ok and max_val is not None:
        return int(max_val)
    return 0

def pegar_maior_valor_geral(layer, nome_campo):
    """ Busca o maior valor de um campo usando agregador nativo """
    if layer.fields().indexOf(nome_campo) == -1:
        return 0
        
    max_val, ok = layer.aggregate(QgsAggregateCalculator.Max, nome_campo)
    if ok and max_val is not None:
        return int(max_val)
    return 0

def extrair_lotes_e_quadras_por_setor():
    project = QgsProject.instance()
    
    # 2. Carrega as camadas do projeto
    iface.messageBar().pushMessage("Aguarde", "Verificando camadas no projeto...", level=Qgis.Info, duration=2)
    
    layer_edif = obter_camada_do_projeto(NOME_CAMADA_EDIF)
    layer_lotes = obter_camada_do_projeto(NOME_CAMADA_LOTES)
    layer_quadras = obter_camada_do_projeto(NOME_CAMADA_QUADRAS)
    layer_setores = obter_camada_do_projeto(NOME_CAMADA_SETORES)
    
    if not all([layer_edif, layer_lotes, layer_quadras, layer_setores]):
        iface.messageBar().pushMessage("Erro de Camada", "Não foi possível encontrar todas as camadas.", level=Qgis.Critical, duration=7)
        return

    # 3. Pede o código do setor fiscal ao usuário
    numero_digitado, ok = QInputDialog.getText(None, "Filtrar Setor", "Digite o código do setor (cod_sf):")
    if not ok or not numero_digitado.strip():
        return
    
    # 4. Busca o setor fiscal correspondente
    expressao = f'"{NOME_CAMPO_SETOR}" = {numero_digitado}'
    request_setor = QgsFeatureRequest().setFilterExpression(expressao)
    setores = list(layer_setores.getFeatures(request_setor))
    
    if not setores:
        iface.messageBar().pushMessage("Erro", f"Nenhum setor encontrado.", level=Qgis.Critical, duration=5)
        return

    setor_selecionado = setores[0]
    valor_sf_sat = setor_selecionado['cod_sf_sat']
    geom_setor = setor_selecionado.geometry()
    bbox_setor = geom_setor.boundingBox()

    # 5. Descobre os maiores valores existentes
    iface.messageBar().pushMessage("Aguarde", f"Calculando contadores e identificadores...", level=Qgis.Info, duration=3)
    
    maior_qf = max(
        pegar_maior_qf_do_setor(layer_quadras, valor_sf_sat),
        pegar_maior_qf_do_setor(layer_lotes, valor_sf_sat)
    )
    proximo_qf = maior_qf + 1

    proximo_id_lote = pegar_maior_valor_geral(layer_lotes, 'id')
    proximo_fid_lote = pegar_maior_valor_geral(layer_lotes, 'fid')
    proximo_id_quadra = pegar_maior_valor_geral(layer_quadras, 'id')
    proximo_fid_quadra = pegar_maior_valor_geral(layer_quadras, 'fid')

    # 6. Criação de Índices Espaciais OTIMIZADA (Apenas o que toca o Bounding Box do Setor)
    request_bbox = QgsFeatureRequest().setFilterRect(bbox_setor)
    
    index_edif = QgsSpatialIndex(layer_edif.getFeatures(request_bbox)) 
    index_lotes = QgsSpatialIndex(layer_lotes.getFeatures(request_bbox))
    index_quadras = QgsSpatialIndex(layer_quadras.getFeatures(request_bbox))

    # Cache de Índices dos Campos para evitar chamar .indexOf() no loop
    idx_q_sq = layer_quadras.fields().indexOf('sq')
    idx_q_sat = layer_quadras.fields().indexOf('cod_sf_sat')
    idx_q_qf = layer_quadras.fields().indexOf('cod_qf')
    idx_q_sf = layer_quadras.fields().indexOf('cod_sf')
    idx_q_id = layer_quadras.fields().indexOf('id')
    idx_q_fid = layer_quadras.fields().indexOf('fid')

    idx_l_sq = layer_lotes.fields().indexOf('sq')
    idx_l_sql = layer_lotes.fields().indexOf('sql')
    idx_l_sat = layer_lotes.fields().indexOf('cod_sf_sat')
    idx_l_qf = layer_lotes.fields().indexOf('cod_qf')
    idx_l_lf = layer_lotes.fields().indexOf('cod_lf')
    idx_l_id = layer_lotes.fields().indexOf('id')
    idx_l_fid = layer_lotes.fields().indexOf('fid')

    idx_e_sql = layer_edif.fields().indexOf('sql')
    idx_e_sqle = layer_edif.fields().indexOf('sqle')
    idx_e_ef = layer_edif.fields().indexOf('cod_ef')

    novas_features_lote = []
    novas_features_quadra = []
    edificacoes_para_atualizar = [] 

    ids_edificacoes_no_setor = index_edif.intersects(bbox_setor)
    edificacoes_isoladas = {}
    
    # 7. Coleta apenas edificações que não tocam lotes/quadras
    for id_edif in ids_edificacoes_no_setor:
        feat_edif = layer_edif.getFeature(id_edif)
        geom_edif = feat_edif.geometry()
        
        if not geom_edif.within(geom_setor):
            continue
            
        bbox_edif = geom_edif.boundingBox()

        # Descarta se já tocar em Lote ou Quadra existente
        toca_lote = any(geom_edif.intersects(layer_lotes.getFeature(id_l).geometry()) for id_l in index_lotes.intersects(bbox_edif))
        if toca_lote: continue
        
        toca_quadra = any(geom_edif.intersects(layer_quadras.getFeature(id_q).geometry()) for id_q in index_quadras.intersects(bbox_edif))
        if toca_quadra: continue

        edificacoes_isoladas[id_edif] = feat_edif

    # 8. Agrupa as edificações que se tocam (Otimizado com Índice Espacial Local)
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
            
            # Checa interseções apenas com vizinhos próximos usando o índice temporário
            vizinhos_ids = index_isoladas.intersects(geom_atual.boundingBox())
            for v_id in vizinhos_ids:
                if v_id not in visitados:
                    outra_feat = edificacoes_isoladas[v_id]
                    if geom_atual.intersects(outra_feat.geometry()):
                        visitados.add(v_id)
                        grupo_atual.append(outra_feat)
                        fila.append(outra_feat)
        
        grupos.append(grupo_atual)

    # 9. Criação de Quadras e Lotes
    str_sf_sat = str(valor_sf_sat).zfill(3)
    
    for grupo in grupos:
        geom_combinada = QgsGeometry(grupo[0].geometry())
        for feat in grupo[1:]:
            geom_combinada = geom_combinada.combine(feat.geometry())

        proximo_id_quadra += 1
        proximo_fid_quadra += 1
        str_qf_atual = str(proximo_qf).zfill(4)
        str_sq_atual = f"{str_sf_sat}{str_qf_atual}"

        nova_feat_quadra = QgsFeature(layer_quadras.fields())
        nova_feat_quadra.setGeometry(geom_combinada)
        
        if idx_q_sq != -1: nova_feat_quadra[idx_q_sq] = str_sq_atual
        if idx_q_sat != -1: nova_feat_quadra[idx_q_sat] = valor_sf_sat
        if idx_q_qf != -1: nova_feat_quadra[idx_q_qf] = proximo_qf
        if idx_q_sf != -1: nova_feat_quadra[idx_q_sf] = int(numero_digitado) 
        if idx_q_id != -1: nova_feat_quadra[idx_q_id] = proximo_id_quadra
        if idx_q_fid != -1: nova_feat_quadra[idx_q_fid] = proximo_fid_quadra
        novas_features_quadra.append(nova_feat_quadra)

        cod_lf_atual = 1 
        
        for feat_edif in grupo:
            proximo_id_lote += 1
            proximo_fid_lote += 1

            str_lf_atual = str(cod_lf_atual).zfill(3)
            str_sql_atual = f"{str_sq_atual}{str_lf_atual}"
            str_sqle_atual = f"{str_sql_atual}01"

            if idx_e_sql != -1: feat_edif[idx_e_sql] = str_sql_atual
            if idx_e_sqle != -1: feat_edif[idx_e_sqle] = str_sqle_atual
            if idx_e_ef != -1: feat_edif[idx_e_ef] = 1
            edificacoes_para_atualizar.append(feat_edif)

            nova_feat_lote = QgsFeature(layer_lotes.fields())
            nova_feat_lote.setGeometry(feat_edif.geometry()) 
            
            if idx_l_sq != -1: nova_feat_lote[idx_l_sq] = str_sq_atual
            if idx_l_sql != -1: nova_feat_lote[idx_l_sql] = str_sql_atual
            if idx_l_sat != -1: nova_feat_lote[idx_l_sat] = valor_sf_sat
            if idx_l_qf != -1: nova_feat_lote[idx_l_qf] = proximo_qf
            if idx_l_lf != -1: nova_feat_lote[idx_l_lf] = cod_lf_atual
            if idx_l_id != -1: nova_feat_lote[idx_l_id] = proximo_id_lote
            if idx_l_fid != -1: nova_feat_lote[idx_l_fid] = proximo_fid_lote
            novas_features_lote.append(nova_feat_lote)

            cod_lf_atual += 1 
            
        proximo_qf += 1

    # 10. Salva as edições
    if novas_features_lote or novas_features_quadra or edificacoes_para_atualizar:
        
        if novas_features_lote:
            layer_lotes.startEditing()
            layer_lotes.addFeatures(novas_features_lote)
            layer_lotes.triggerRepaint()

        if novas_features_quadra:
            layer_quadras.startEditing()
            layer_quadras.addFeatures(novas_features_quadra)
            layer_quadras.triggerRepaint()

        if edificacoes_para_atualizar:
            layer_edif.startEditing()
            # updateFeatures() em lote (batch) é mais eficiente que um loop de updateFeature()
            for feat in edificacoes_para_atualizar:
                layer_edif.updateFeature(feat) 
            layer_edif.triggerRepaint()

        iface.messageBar().pushMessage(
            "Sucesso", 
            f"Processado: {len(novas_features_quadra)} Quadras, {len(novas_features_lote)} Lotes e {len(edificacoes_para_atualizar)} Edificações atualizadas!", 
            level=Qgis.Success, 
            duration=7
        )
    else:
        iface.messageBar().pushMessage("Concluído", "Nenhuma edificação isolada precisou ser alterada.", level=Qgis.Info, duration=5)

extrair_lotes_e_quadras_por_setor()