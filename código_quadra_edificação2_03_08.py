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

    idx_q_sq, idx_q_sat, idx_q_qf = [layer_quadras.fields().indexOf(f) for f in ['sq', 'cod_sf_sat', 'cod_qf']]
    idx_l_sq, idx_l_sql, idx_l_sat, idx_l_qf, idx_l_lf = [layer_lotes.fields().indexOf(f) for f in ['sq', 'sql', 'cod_sf_sat', 'cod_qf', 'cod_lf']]
    idx_e_sql, idx_e_sqle = [layer_edif.fields().indexOf(f) for f in ['sql', 'sqle']]

    # Caches
    req_quadras = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_q_sq, idx_q_sat, idx_q_qf])
    quadras_in_bbox = {}
    mapa_quadras_por_sq = {} 
    index_quadras = QgsSpatialIndex()
    for feat in layer_quadras.getFeatures(req_quadras):
        quadras_in_bbox[feat.id()] = feat
        index_quadras.addFeature(feat)
        
        sq = feat.attribute(idx_q_sq)
        if sq not in (None, NULL):
            mapa_quadras_por_sq[sq] = feat

    req_lotes = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_l_sq, idx_l_sql, idx_l_lf])
    lotes_in_bbox = {}
    index_lotes = QgsSpatialIndex()
    
    max_lf_dict = {}
    for feat in layer_lotes.getFeatures(req_lotes):
        lotes_in_bbox[feat.id()] = feat
        index_lotes.addFeature(feat)
        
        sq_val = feat.attribute(idx_l_sq)
        lf_val = feat.attribute(idx_l_lf)
        if sq_val not in (None, NULL) and lf_val not in (None, NULL):
            try:
                lf_int = int(lf_val)
                if sq_val not in max_lf_dict or lf_int > max_lf_dict[sq_val]:
                    max_lf_dict[sq_val] = lf_int
            except ValueError:
                pass

    req_edif = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_e_sql, idx_e_sqle])
    edificacoes_in_bbox = {}
    index_edif = QgsSpatialIndex()
    for feat in layer_edif.getFeatures(req_edif):
        edificacoes_in_bbox[feat.id()] = feat
        index_edif.addFeature(feat)

    ids_edificacoes_no_setor = index_edif.intersects(bbox_setor)
    
    edificacoes_por_quadra = {}
    mapa_edificacoes_para_atualizar = {}
    
    # =========================================================================
    # Regras de Negócio Espaciais
    # =========================================================================
    for id_edif in ids_edificacoes_no_setor:
        feat_edif = edificacoes_in_bbox[id_edif]
        
        val_sql = feat_edif.attribute(idx_e_sql)
        val_sqle = feat_edif.attribute(idx_e_sqle)
        
        sql_preenchido = val_sql not in (None, NULL) and str(val_sql).strip() != ''
        sqle_preenchido = val_sqle not in (None, NULL) and str(val_sqle).strip() != ''
        
        if sql_preenchido or sqle_preenchido:
            continue
            
        geom_edif = feat_edif.geometry()
        
        # Validação extra para evitar que geometria corrompida trave o PostGIS
        if not geom_edif.isGeosValid():
            print(f"Edificação ID {feat_edif.id()} ignorada: Geometria inválida/corrompida.")
            continue
        
        # Evita que o novo desenho ultrapasse o setor fiscal indicado
        if not geom_edif.within(geom_setor):
            continue
            
        bbox_edif = geom_edif.boundingBox()

        # ---------------------------------------------------------------------
        # NOVA TRAVA: Edificação vs Edificação (Validação Topológica)
        # ---------------------------------------------------------------------
        sobrepoe_outra_edificacao = False
        for id_outra in index_edif.intersects(bbox_edif):
            if id_outra == feat_edif.id(): 
                continue
                
            feat_outra = edificacoes_in_bbox[id_outra]
            geom_outra = feat_outra.geometry()
            
            # Se intersecta, mas NÃO é apenas um toque de linha de borda = SOBREPOSIÇÃO REAL
            if geom_edif.intersects(geom_outra) and not geom_edif.touches(geom_outra):
                sobrepoe_outra_edificacao = True
                break
                    
        if sobrepoe_outra_edificacao:
            #print(f"Edificação ID {feat_edif.id()} ignorada: Sobrepõe outra edificação.")
            continue
        # ---------------------------------------------------------------------

        # 2. Verifica contato ou sobreposição com Lotes já desenhados
        sobrepoe_lote_invalido = False
        lotes_adjacentes = []
        lote_pai = None 
        
        for id_l in index_lotes.intersects(bbox_edif):
            feat_lote_exist = lotes_in_bbox[id_l]
            geom_lote_exist = feat_lote_exist.geometry()
            
            if geom_edif.within(geom_lote_exist):
                lote_pai = feat_lote_exist
                break
                
            elif geom_edif.intersects(geom_lote_exist):
                # Usando o mesmo princípio topológico seguro
                if not geom_edif.touches(geom_lote_exist):
                    sobrepoe_lote_invalido = True
                    break
                else:
                    # Eles apenas dividem muro, o que é totalmente permitido
                    lotes_adjacentes.append(feat_lote_exist)
        
        if lote_pai: # Caso a edificação esteja totalmente dentro de um lote, mas não possua SQL
            sql_herdado = lote_pai.attribute(idx_l_sql)
            if sql_herdado not in (None, NULL) and idx_e_sql != -1:
                mapa_edificacoes_para_atualizar[feat_edif.id()] = {idx_e_sql: sql_herdado}
            continue

        if sobrepoe_lote_invalido: 
            print(f"Edificação ID {feat_edif.id()} ignorada: Sobrepõe um lote existente.")
            continue

        # 3. Descobrir a qual Quadra essa edificação pertence
        quadras_contendo = []
        quadras_intersectadas_parcialmente = []
        
        for id_q in index_quadras.intersects(bbox_edif):
            feat_q = quadras_in_bbox[id_q]
            geom_q = feat_q.geometry()
            
            if geom_edif.within(geom_q):
                quadras_contendo.append(feat_q)
            elif geom_edif.intersects(geom_q):
                # Se invade a quadra vizinha (não apenas encosta na linha divisória)
                if not geom_edif.touches(geom_q):
                    quadras_intersectadas_parcialmente.append(feat_q)
        
        quadra_valida = None
        
        if len(quadras_contendo) == 1:
            quadra_valida = quadras_contendo[0]
            
        elif len(quadras_contendo) == 0 and len(lotes_adjacentes) > 0:
            for lote_adj in lotes_adjacentes:
                sq_lote = lote_adj.attribute(idx_l_sq)
                if sq_lote not in (None, NULL):
                    quadra_valida = mapa_quadras_por_sq.get(sq_lote) 
                if quadra_valida:
                    break

        if not quadra_valida:
            continue 

        # Trava de segurança para invasão de outras quadras
        invade_quadra_errada = False
        for feat_q_invadida in quadras_intersectadas_parcialmente:
            if feat_q_invadida.id() != quadra_valida.id():
                invade_quadra_errada = True
                break
                
        if invade_quadra_errada:
            print(f"Edificação ID {feat_edif.id()} ignorada: Invade a quadra vizinha.")
            continue

        geom_q_valida = quadra_valida.geometry()
        
        if not geom_q_valida.within(geom_setor):
            continue
        
        # Se a edificação vazar o limite da quadra principal, bloqueia
        if not geom_edif.within(geom_q_valida):
            print(f"Edificação ID {feat_edif.id()} ignorada: Vaza os limites da sua própria Quadra.")
            continue

        q_id = quadra_valida.id()
        if q_id not in edificacoes_por_quadra:
            edificacoes_por_quadra[q_id] = []
        edificacoes_por_quadra[q_id].append(feat_edif)

    # =========================================================================
    # Criação de Lotes e Mapeamento de Atualizações
    # =========================================================================
    novas_features_lote = []
    
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
            str_lf_atual = str(cod_lf_atual).zfill(4)
            str_sql_atual = f"{str_sq}{str_lf_atual}" 
            
            print(f"Sucesso: Processando Edificação ID {feat_edif.id()} -> Gerando Lote e SQL {str_sql_atual}")
            
            atributos_novos = {}
            if idx_e_sql != -1: atributos_novos[idx_e_sql] = str_sql_atual
            
            mapa_edificacoes_para_atualizar[feat_edif.id()] = atributos_novos

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

            nova_feat_lote = QgsFeature(layer_lotes.fields())
            nova_feat_lote.setGeometry(geom_lote)
            
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
            f"Processado: {len(novas_features_lote)} Lote(s) criado(s) e {len(mapa_edificacoes_para_atualizar)} Edificação(ões) atualizada(s)!",
            level=Qgis.Success,
            duration=7
        )
    else:
        iface.messageBar().pushMessage("Concluído", "Nenhuma edificação atendeu aos critérios rigorosos para virar lote.", level=Qgis.Info, duration=5)

extrair_lotes_por_quadra_existente()