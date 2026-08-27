from qgis.core import (
    QgsProject,
    QgsFeature,
    QgsFeatureRequest,
    QgsSpatialIndex,
    QgsGeometry,
    Qgis,
    QgsWkbTypes,
    NULL,
    QgsGeometryEngine
)
from qgis.utils import iface

# ==========================================
# 1. NOMES DAS CAMADAS NO PROJETO DO QGIS
# ==========================================
NOME_CAMADA_EDIF = 'edificacao_fiscal_rev1g'
NOME_CAMADA_LOTES = 'ct_lote_fiscal'
NOME_CAMADA_QUADRAS = 'ct_quadra_fiscal'
NOME_CAMADA_SETORES = 'ct_setor_fiscal'

NOME_CAMPO_SETOR = 'cod_sf'

def obter_camada_do_projeto(nome_camada):
    camadas = QgsProject.instance().mapLayersByName(nome_camada)
    return camadas[0] if camadas else None

def extrair_lotes_todos_os_setores():
    iface.messageBar().pushMessage("Aguarde", "Verificando camadas no projeto...", level=Qgis.Info, duration=2)
    
    layer_edif = obter_camada_do_projeto(NOME_CAMADA_EDIF)
    layer_lotes = obter_camada_do_projeto(NOME_CAMADA_LOTES)
    layer_quadras = obter_camada_do_projeto(NOME_CAMADA_QUADRAS)
    layer_setores = obter_camada_do_projeto(NOME_CAMADA_SETORES)
    
    if not all([layer_edif, layer_lotes, layer_quadras, layer_setores]):
        iface.messageBar().pushMessage("Erro", "Não foi possível encontrar todas as camadas.", level=Qgis.Critical, duration=7)
        return

    # Pega todos os setores da camada
    setores = list(layer_setores.getFeatures())
    if not setores:
        iface.messageBar().pushMessage("Erro", "Nenhum setor encontrado na camada.", level=Qgis.Critical, duration=5)
        return

    total_lotes_criados = 0
    total_edif_atualizadas = 0

    # Abre a edição das camadas uma única vez para todo o processo
    layer_lotes.startEditing()
    layer_edif.startEditing()
    layer_quadras.startEditing()

    wkb_lotes = layer_lotes.wkbType()
    is_multi_lote = QgsWkbTypes.isMultiType(wkb_lotes)

    idx_q_sq, idx_q_sat, idx_q_qf = [layer_quadras.fields().indexOf(f) for f in ['sq', 'cod_sf_sat', 'cod_qf']]
    idx_l_sq, idx_l_sql, idx_l_sat, idx_l_qf, idx_l_lf = [layer_lotes.fields().indexOf(f) for f in ['sq', 'sql', 'cod_sf_sat', 'cod_qf', 'cod_lf']]
    idx_e_sql, idx_e_sqle = [layer_edif.fields().indexOf(f) for f in ['sql', 'sqle']]

    # Loop para percorrer CADA SETOR encontrado
    for idx_setor, setor_selecionado in enumerate(setores):
        cod_sf_atual = setor_selecionado.attribute(NOME_CAMPO_SETOR)
        print(f"Processando Setor ({idx_setor + 1}/{len(setores)}) - Código: {cod_sf_atual}")

        geom_setor = setor_selecionado.geometry()
        if geom_setor.isEmpty():
            continue
            
        bbox_setor = geom_setor.boundingBox()

        engine_setor = QgsGeometry.createGeometryEngine(geom_setor.constGet())
        engine_setor.prepareGeometry()

        # Caches espaciais baseados na bbox do setor atual
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
        cache_quadras_validas = {}
        
        # Regras de Negócio Espaciais por Setor
        for id_edif in ids_edificacoes_no_setor:
            feat_edif = edificacoes_in_bbox[id_edif]
            
            val_sql = feat_edif.attribute(idx_e_sql)
            val_sqle = feat_edif.attribute(idx_e_sqle)
            
            if (val_sql not in (None, NULL) and str(val_sql).strip() != '') or \
               (val_sqle not in (None, NULL) and str(val_sqle).strip() != ''):
                continue
                
            geom_edif = feat_edif.geometry()
            if not geom_edif.isGeosValid():
                continue
            
            engine_edif = QgsGeometry.createGeometryEngine(geom_edif.constGet())
            engine_edif.prepareGeometry()

            # O setor deve conter 100% da edificação. Se vazar para fora, ignora.
            if not engine_setor.contains(geom_edif.constGet()):
                print(f"Edificação ID {feat_edif.id()} ignorada: Cruza a divisa do setor {cod_sf_atual}.")
                continue
                
            bbox_edif = geom_edif.boundingBox()

            # Edificação vs Edificação
            sobrepoe_outra_edificacao = False
            for id_outra in index_edif.intersects(bbox_edif):
                if id_outra == feat_edif.id(): 
                    continue
                feat_outra = edificacoes_in_bbox[id_outra]
                geom_outra = feat_outra.geometry().constGet()
                if engine_edif.intersects(geom_outra) and not engine_edif.touches(geom_outra):
                    sobrepoe_outra_edificacao = True
                    break
                        
            if sobrepoe_outra_edificacao:
                continue

            # Lotes
            sobrepoe_lote_invalido = False
            lotes_adjacentes = []
            lote_pai = None 
            
            for id_l in index_lotes.intersects(bbox_edif):
                feat_lote_exist = lotes_in_bbox[id_l]
                geom_lote_exist = feat_lote_exist.geometry().constGet()
                
                if engine_edif.within(geom_lote_exist):
                    lote_pai = feat_lote_exist
                    break
                elif engine_edif.intersects(geom_lote_exist):
                    if not engine_edif.touches(geom_lote_exist):
                        sobrepoe_lote_invalido = True
                        break
                    else:
                        lotes_adjacentes.append(feat_lote_exist)
            
            if lote_pai:
                sql_herdado = lote_pai.attribute(idx_l_sql)
                if sql_herdado not in (None, NULL) and idx_e_sql != -1:
                    mapa_edificacoes_para_atualizar[feat_edif.id()] = {idx_e_sql: sql_herdado}
                continue

            if sobrepoe_lote_invalido: 
                continue

            # Quadras
            quadras_contendo = []
            quadras_intersectadas_parcialmente = []
            
            for id_q in index_quadras.intersects(bbox_edif):
                feat_q = quadras_in_bbox[id_q]
                geom_q = feat_q.geometry().constGet()
                
                if engine_edif.within(geom_q):
                    quadras_contendo.append(feat_q)
                elif engine_edif.intersects(geom_q):
                    quadras_intersectadas_parcialmente.append(feat_q)
            
            quadra_valida = None
            atribuida_por_proximidade = False
            
            if len(quadras_contendo) == 1:
                quadra_valida = quadras_contendo[0]
            elif len(quadras_contendo) == 0 and len(lotes_adjacentes) > 0:
                for lote_adj in lotes_adjacentes:
                    sq_lote = lote_adj.attribute(idx_l_sq)
                    if sq_lote not in (None, NULL):
                        quadra_valida = mapa_quadras_por_sq.get(sq_lote) 
                    if quadra_valida:
                        break
            
            # 1. Nova Regra: Se a edificação invadir a rua ou tocar a borda de APENAS UMA quadra
            if not quadra_valida and len(quadras_intersectadas_parcialmente) == 1:
                quadra_valida = quadras_intersectadas_parcialmente[0]
                atribuida_por_proximidade = True

            # 2. Nova Regra: Se ela estiver 100% "isolada" (como na imagem, sem encostar em nada)
            if not quadra_valida and len(quadras_intersectadas_parcialmente) == 0:
                menor_distancia = float('inf')
                quadra_mais_proxima = None
                
                for q_id_bbox, feat_q_bbox in quadras_in_bbox.items():
                    geom_q_bbox = feat_q_bbox.geometry().constGet()
                    dist = engine_edif.distance(geom_q_bbox)
                    
                    if dist < menor_distancia:
                        menor_distancia = dist
                        quadra_mais_proxima = feat_q_bbox
                
                if quadra_mais_proxima:
                    quadra_valida = quadra_mais_proxima
                    atribuida_por_proximidade = True

            # Diagnóstico 1: Se mesmo assim não achou quadra
            if not quadra_valida:
                print(f"Edificação ID {feat_edif.id()} ignorada: Nenhuma quadra encontrada por proximidade.")
                continue 

            q_id = quadra_valida.id()
            if q_id not in cache_quadras_validas:
                geom_q_valida_teste = quadra_valida.geometry()
                # CORREÇÃO CRÍTICA: 'intersects' em vez de 'contains' para evitar erros de borda
                cache_quadras_validas[q_id] = engine_setor.intersects(geom_q_valida_teste.constGet())
                
            # Diagnóstico 2: Quadra inválida
            if not cache_quadras_validas[q_id]:
                print(f"Edificação ID {feat_edif.id()} ignorada: A quadra {q_id} não pertence/intersecta o setor.")
                continue
            
            geom_q_valida_const = quadra_valida.geometry().constGet()

            # =================================================================
            # NOVA LÓGICA: Expandir a Quadra para cobrir a Edificação
            # =================================================================
            geom_q_atual = quadra_valida.geometry()
            geom_edif_atual = feat_edif.geometry()
            
            # Calcula o vão vazio entre a quadra e a edificação
            distancia_vao = geom_q_atual.distance(geom_edif_atual)
            
            if distancia_vao > 0:
                # Aplica um buffer ligeiramente maior que o vão (+ 0.1) para garantir sobreposição.
                # ATENÇÃO: O valor 0.1 assume que seu projeto está em METROS (SIRGAS 2000 / UTM).
                geom_para_unir = geom_edif_atual.buffer(distancia_vao + 0.001, 5)
            else:
                geom_para_unir = geom_edif_atual
            
            # 1. Faz a união temporária na memória (agora elas se tocam e formam polígono único)
            nova_geom_quadra = geom_q_atual.combine(geom_para_unir)
            
            # (CORREÇÃO POSTGIS: Forçar Polígono Simples)
            wkb_quadra = layer_quadras.wkbType()
            is_multi_quadra = QgsWkbTypes.isMultiType(wkb_quadra)
            
            if not is_multi_quadra and nova_geom_quadra.isMultipart():
                maior_area_q = -1
                geom_simples_q = nova_geom_quadra
                for part in nova_geom_quadra.asGeometryCollection():
                    if part.area() > maior_area_q:
                        maior_area_q = part.area()
                        geom_simples_q = part
                nova_geom_quadra = geom_simples_q

            # 2. TRAVA DE SEGURANÇA MÁXIMA: Consulta a camada inteira
            geom_q_teste = QgsGeometry(nova_geom_quadra)
            chocou_com_outra_quadra = False
            
            # Puxa direto da camada do projeto qualquer quadra que cruze essa nova área
            req_colisao = QgsFeatureRequest().setFilterRect(geom_q_teste.boundingBox())
            
            for feat_outra_q in layer_quadras.getFeatures(req_colisao):
                if feat_outra_q.id() == q_id: 
                    continue # Ignora a si mesma
                
                geom_outra = QgsGeometry(feat_outra_q.geometry())
                
                if geom_q_teste.intersects(geom_outra) and not geom_q_teste.touches(geom_outra):
                    chocou_com_outra_quadra = True
                    break
            
            if chocou_com_outra_quadra:
                print(f"Edificação ID {feat_edif.id()} ignorada: Invasão detectada com a quadra vizinha ID {feat_outra_q.id()}.")
                continue
            
            # 3. ATUALIZAÇÃO E SINCRONIA DO ÍNDICE (O Segredo para evitar o erro)
            # Retira a geometria antiga da quadra do radar
            index_quadras.deleteFeature(quadra_valida)
            
            # Efetiva as alterações no QGIS e na variável
            layer_quadras.changeGeometry(q_id, nova_geom_quadra)
            quadra_valida.setGeometry(nova_geom_quadra)
            
            # Adiciona a quadra de volta ao radar, agora com o tamanho novo
            index_quadras.addFeature(quadra_valida)
            # =================================================================

            if q_id not in edificacoes_por_quadra:
                edificacoes_por_quadra[q_id] = []
            edificacoes_por_quadra[q_id].append(feat_edif)

        # Criação de Lotes para o setor atual
        novas_features_lote = []
        for q_id, lista_edif in edificacoes_por_quadra.items():
            quadra = quadras_in_bbox[q_id]
            str_sq = str(quadra.attribute(idx_q_sq)).strip() if quadra.attribute(idx_q_sq) not in (None, NULL) else ""
            cod_qf = quadra.attribute(idx_q_qf)
            valor_sf_sat = quadra.attribute(idx_q_sat)
            
            if not str_sq:
                continue
                
            cod_lf_atual = max_lf_dict.get(str_sq, 0) + 1
            
            for feat_edif in lista_edif: 
                str_lf_atual = str(cod_lf_atual).zfill(4)
                str_sql_atual = f"{str_sq}{str_lf_atual}" 
                
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

        # Acumula as inserções/atualizações na sessão de edição aberta
        if novas_features_lote:
            layer_lotes.addFeatures(novas_features_lote)
            total_lotes_criados += len(novas_features_lote)

        if mapa_edificacoes_para_atualizar:
            for fid, atributos in mapa_edificacoes_para_atualizar.items():
                feat_update = layer_edif.getFeature(fid)
                if feat_update.isValid():
                    for idx_campo, novo_valor in atributos.items():
                        feat_update.setAttribute(idx_campo, novo_valor)
                    
                    sucesso = layer_edif.updateFeature(feat_update)
                    if not sucesso:
                        valores_debug = [f"Valor: '{v}' (Tamanho: {len(str(v))})" for v in atributos.values()]
                        print(f"ERRO: O QGIS bloqueou a atualização da Edificação {fid}. {valores_debug}")
                        
            total_edif_atualizadas += len(mapa_edificacoes_para_atualizar)

    # Atualiza a tela ao finalizar todos os setores
    layer_lotes.triggerRepaint()
    layer_edif.triggerRepaint()

    if total_lotes_criados > 0 or total_edif_atualizadas > 0:
        iface.messageBar().pushMessage(
            "Sucesso Geral",
            f"Processo concluído para todos os setores! {total_lotes_criados} Lote(s) criado(s) e {total_edif_atualizadas} Edificação(ões) atualizada(s). Revise e salve manualmente.",
            level=Qgis.Success,
            duration=10
        )
    else:
        iface.messageBar().pushMessage("Concluído", "Nenhuma edificação atendeu aos critérios nos setores analisados.", level=Qgis.Info, duration=5)

extrair_lotes_todos_os_setores()